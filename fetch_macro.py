"""
fetch_macro.py - Series macro-financieras para la pestaña «Contexto macro-financiero» de la app.

Genera data/macro.csv en formato largo (una fila por variable y mes):
    fecha (1er dia del mes), variable, valor, etiqueta, unidad, fuente, tipo, agregacion

Fuentes:
  * BCRA, API Estadisticas Monetarias v4.0 (series diarias -> se agregan a mes).
  * datos.gob.ar, API de Series de Tiempo (Balance Cambiario y comercio exterior del INDEC, ya mensual; terminos de intercambio, trimestral).
  * ArgentinaDatos (terceros, NO oficial): dolar CCL, MEP (bolsa), blue y riesgo pais. Si falla, se conserva lo ya guardado.
  * FRED (St. Louis Fed, CSV abierto): IPC de EE.UU. (CPIAUCSL) y precios de cobre, soja y WTI. Cuenta como fuente de terceros.
  * data/actuals_monthly.csv (lo genera fetch_actuals.py antes en el workflow): IPC mensual de Argentina, que se encadena en un indice.
  * Derivadas: brechas CCL / MEP / blue contra el mayorista oficial (BCRA id 5) y tipo de cambio real bilateral con EE.UU.

Politica de errores (pensada para el workflow mensual):
  * Si falla una serie OFICIAL (BCRA / datos.gob.ar): no se escribe nada y el script termina con codigo 1
    (el Action falla y no commitea datos a medias).
  * Si falla una serie de TERCEROS (ArgentinaDatos, FRED, IPC local, comercio exterior INDEC): se conservan las filas previas de data/macro.csv y se avisa por pantalla.

Solo usa libreria estandar + pandas. Uso:
    python fetch_macro.py                 # actualiza data/macro.csv
    python fetch_macro.py --only COMPRAS_BCRA,RESERVAS_BRUTAS   # depuracion (no escribe, solo muestra)
Variable de entorno opcional BCRA_CA_BUNDLE: ruta a un bundle de certificados si el entorno no valida la cadena
de la API del BCRA (no se desactiva nunca la verificacion TLS).
"""
import argparse
import io
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"
OUT = DATA / "macro.csv"
START = "2016-06-01"  # primer relevamiento del REM
UA = "rem-macro-fetch/1.0 (+https://github.com/mau1878/REM)"
BCRA = "https://api.bcra.gob.ar/estadisticas/v4.0/monetarias"
DATOS = "https://apis.datos.gob.ar/series/api/series/"
ARGDATOS = "https://api.argentinadatos.com/v1"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv"
ACTUALS = DATA / "actuals_monthly.csv"  # lo escribe fetch_actuals.py (serie IPC_MENSUAL, var. % mensual)
BLANDAS = ("argdatos", "fred", "local")  # fuentes cuya falla conserva lo previo en vez de abortar
ICA = "datos.gob.ar (INDEC, Intercambio Comercial Argentino)"
MIN_MESES = 12  # una serie con menos meses que esto se considera fallida (respuesta vacia o truncada)

# tipo: stock | flujo | precio | indicador  (la pestaña lo usa para ofrecer transformaciones con sentido)
# agg: como se pasa de diario a mensual (last = ultimo dato del mes; sum; mean; none = ya es mensual)
SERIES = [
    # ---- BCRA (oficial)
    dict(var="RESERVAS_BRUTAS", src="bcra", id=1, agg="last", tipo="stock", unidad="USD mn",
         etiqueta="Reservas internacionales (fin de mes)"),
    dict(var="COMPRAS_BCRA", src="bcra", id=78, agg="sum", tipo="flujo", unidad="USD mn",
         etiqueta="Compras de divisas del BCRA (variación de reservas por compra de divisas, suma del mes)"),
    dict(var="BASE_MONETARIA", src="bcra", id=15, agg="last", tipo="stock", unidad="ARS mn",
         etiqueta="Base monetaria (fin de mes)"),
    dict(var="M2", src="bcra", id=109, agg="last", tipo="stock", unidad="ARS mn", etiqueta="M2 (fin de mes)"),
    dict(var="DEPOSITOS_USD_PRIV", src="bcra", id=108, agg="last", tipo="stock", unidad="USD mn",
         etiqueta="Depósitos en dólares del sector privado no financiero (fin de mes)"),
    dict(var="DEPOSITOS_PRIV_TOTAL", src="bcra", id=1526, agg="last", tipo="stock", unidad="ARS mn",
         etiqueta="Depósitos totales del sector privado (fin de mes)"),
    # ---- datos.gob.ar / Balance Cambiario (oficial, mensual, USD mn)
    dict(var="FAE_PRIV_NETA", src="datos", id="182.1_C_K_FINC_CINC_0_M_56", agg="none", tipo="flujo", unidad="USD mn",
         etiqueta="Formación de activos externos del sector privado no financiero (neta: ingresos − egresos; negativo = salida)"),
    dict(var="FAE_PRIV_INGRESOS", src="datos", id="182.1_C_K_FINC_CINC_0_M_57", agg="none", tipo="flujo", unidad="USD mn",
         etiqueta="Formación de activos externos del sector privado: ingresos"),
    dict(var="FAE_PRIV_EGRESOS", src="datos", id="182.1_C_K_FINC_CINC_NO_FINAN_0_M_56", agg="none", tipo="flujo",
         unidad="USD mn", etiqueta="Formación de activos externos del sector privado: egresos"),
    dict(var="CC_CAMBIARIA", src="datos", id="182.1_TOTAL_CUENRIA_0_M_32", agg="none", tipo="flujo", unidad="USD mn",
         etiqueta="Total cuenta corriente cambiaria"),
    dict(var="CC_BIENES", src="datos", id="182.1_CUENTA_CORNES_0_M_39", agg="none", tipo="flujo", unidad="USD mn",
         etiqueta="Cuenta corriente cambiaria: total bienes"),
    dict(var="CC_SERVICIOS", src="datos", id="182.1_CUENTA_CORIOS_0_M_42", agg="none", tipo="flujo", unidad="USD mn",
         etiqueta="Cuenta corriente cambiaria: total servicios"),
    dict(var="DOLAR_OFICIAL", src="bcra", id=5, agg="mean", tipo="precio", unidad="ARS por USD",
         etiqueta="Dólar mayorista oficial, Com. A 3500 (promedio del mes)"),
    # ---- terceros (no oficial)
    dict(var="DOLAR_CCL", src="argdatos", id="contadoconliqui", agg="mean", tipo="precio", unidad="ARS por USD",
         etiqueta="Dólar CCL (promedio del mes, precio de venta)"),
    dict(var="DOLAR_MEP", src="argdatos", id="bolsa", agg="mean", tipo="precio", unidad="ARS por USD",
         etiqueta="Dólar MEP (promedio del mes, precio de venta; datos desde oct-2018)"),
    dict(var="DOLAR_BLUE", src="argdatos", id="blue", agg="mean", tipo="precio", unidad="ARS por USD",
         etiqueta="Dólar blue (promedio del mes, precio de venta)"),
    dict(var="IPC_EEUU", src="fred", id="CPIAUCSL", agg="none", tipo="precio", unidad="índice 1982-84=100",
         etiqueta="IPC de EE.UU., todos los ítems urbanos, desestacionalizado (índice, FRED CPIAUCSL; meses sin publicar se interpolan)"),
    dict(var="IPC_ARG", src="local", id="IPC_MENSUAL", agg="none", tipo="precio", unidad="índice (ene-2016 = 100)",
         etiqueta="IPC de Argentina: índice encadenado desde la variación mensual (BCRA / INDEC)"),
    # ---- comercio exterior (INDEC, Intercambio Comercial Argentino, via datos.gob.ar; mensual, USD mn). "soft": si falla, se conserva lo previo
    dict(var="EXPO_TOTAL", src="datos", id="74.3_IET_0_M_16", agg="none", tipo="flujo", unidad="USD mn", soft=True, fuente=ICA,
         etiqueta="Exportaciones de bienes, total (ICA)"),
    dict(var="IMPO_TOTAL", src="datos", id="74.3_IIT_0_M_25", agg="none", tipo="flujo", unidad="USD mn", soft=True, fuente=ICA,
         etiqueta="Importaciones de bienes, total (ICA)"),
    dict(var="EXPO_ENERGIA", src="datos", id="75.3_ITCE_0_M_30", agg="none", tipo="flujo", unidad="USD mn", soft=True, fuente=ICA,
         etiqueta="Exportaciones de combustibles y energía (FOB)"),
    dict(var="IMPO_COMBUSTIBLES", src="datos", id="74.3_IICL_0_M_42", agg="none", tipo="flujo", unidad="USD mn", soft=True, fuente=ICA,
         etiqueta="Importaciones de combustibles y lubricantes"),
    dict(var="EXPO_PRIMARIOS", src="datos", id="74.3_IEPP_0_M_35", agg="none", tipo="flujo", unidad="USD mn", soft=True, fuente=ICA,
         etiqueta="Exportaciones de productos primarios"),
    dict(var="EXPO_MOA", src="datos", id="74.3_IEMOA_0_M_48", agg="none", tipo="flujo", unidad="USD mn", soft=True, fuente=ICA,
         etiqueta="Exportaciones de manufacturas de origen agropecuario"),
    dict(var="EXPO_COBRE", src="datos", id="75.3_IEMCC_0_M_44", agg="none", tipo="flujo", unidad="USD mn", soft=True, fuente=ICA,
         etiqueta="Exportaciones de mineral de cobre y concentrados (FOB)"),
    dict(var="EXPO_PETROLEO", src="datos", id="75.3_IPC_0_M_18", agg="none", tipo="flujo", unidad="USD mn", soft=True, fuente=ICA,
         etiqueta="Exportaciones de petróleo crudo (FOB)"),
    dict(var="EXPO_GAS", src="datos", id="75.3_GPOH_0_M_32", agg="none", tipo="flujo", unidad="USD mn", soft=True, fuente=ICA,
         etiqueta="Exportaciones de gas de petróleo y otros hidrocarburos (FOB)"),
    dict(var="EXPO_METALES_PRECIOSOS", src="datos", id="75.3_IPMP_0_M_29", agg="none", tipo="flujo", unidad="USD mn", soft=True, fuente=ICA,
         etiqueta="Exportaciones de piedras y metales preciosos (FOB; en la práctica, sobre todo oro y plata)"),
    # ---- terminos de intercambio (INDEC, TRIMESTRAL: la fecha es el inicio del trimestre)
    dict(var="TERMINOS_INTERCAMBIO", src="datos", id="82.2_ITI_2004_T_27", agg="none", tipo="indicador", unidad="índice 2004=100",
         soft=True, fuente="datos.gob.ar (INDEC, términos de intercambio)",
         etiqueta="Términos de intercambio (índice 2004=100, trimestral)"),
    # ---- precios internacionales (FRED, mensual, USD)
    dict(var="PRECIO_COBRE", src="fred", id="PCOPPUSDM", agg="none", tipo="precio", unidad="USD por tonelada",
         etiqueta="Precio internacional del cobre (FRED/FMI, promedio mensual)"),
    dict(var="PRECIO_SOJA", src="fred", id="PSOYBUSDM", agg="none", tipo="precio", unidad="USD por tonelada",
         etiqueta="Precio internacional de la soja (FRED/FMI, promedio mensual)"),
    dict(var="PRECIO_WTI", src="fred", id="MCOILWTICO", agg="none", tipo="precio", unidad="USD por barril",
         etiqueta="Petróleo WTI (FRED, promedio mensual)"),
    dict(var="RIESGO_PAIS", src="argdatos", id="riesgo-pais", agg="last", tipo="indicador", unidad="pb",
         etiqueta="Riesgo país (fin de mes)"),
]
# derivadas: brecha contra el mayorista oficial, calculada dia a dia sobre dias habiles y luego promediada por mes
BRECHAS = [
    dict(var="BRECHA_CCL", base="DOLAR_CCL", agg="mean", tipo="indicador", unidad="%", src="derivada",
         etiqueta="Brecha entre dólar CCL y mayorista oficial (promedio del mes)"),
    dict(var="BRECHA_MEP", base="DOLAR_MEP", agg="mean", tipo="indicador", unidad="%", src="derivada",
         etiqueta="Brecha entre dólar MEP y mayorista oficial (promedio del mes)"),
    dict(var="BRECHA_BLUE", base="DOLAR_BLUE", agg="mean", tipo="indicador", unidad="%", src="derivada",
         etiqueta="Brecha entre dólar blue y mayorista oficial (promedio del mes)"),
]
# tipo de cambio real BILATERAL con EE.UU.: dolar * IPC EE.UU. / IPC Argentina, reescalado para que el promedio de todo el
# periodo comun valga 100 (100 = nivel promedio 2016-hoy; mas alto = dolar mas caro en terminos reales; mas bajo = mas barato)
TCR = [
    dict(var="TCR_OFICIAL", base="DOLAR_OFICIAL", agg="none", tipo="precio", unidad="índice (promedio del período = 100)",
         src="derivada", fuente="Calculada: mayorista (BCRA) × IPC EE.UU. (FRED) ÷ IPC Argentina (BCRA/INDEC)",
         etiqueta="Tipo de cambio real bilateral con EE.UU., dólar mayorista oficial (promedio del período = 100)"),
    dict(var="TCR_CCL", base="DOLAR_CCL", agg="none", tipo="precio", unidad="índice (promedio del período = 100)",
         src="derivada", fuente="Calculada: CCL (ArgentinaDatos) × IPC EE.UU. (FRED) ÷ IPC Argentina (BCRA/INDEC)",
         etiqueta="Tipo de cambio real bilateral con EE.UU., dólar CCL (promedio del período = 100)"),
]
OFICIAL = "DOLAR_OFICIAL"  # mayorista de referencia (insumo de las brechas)


# ---------------------------------------------------------------- HTTP
INSECURE = False  # solo con --insecure; afecta unicamente a api.bcra.gob.ar (cadena SSL incompleta)


def http_json(url, params=None, retries=4, timeout=60, texto=False):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    cafile = os.environ.get("BCRA_CA_BUNDLE")
    ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    if INSECURE and urllib.parse.urlparse(url).hostname == "api.bcra.gob.ar":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/csv" if texto else "application/json"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                cuerpo = r.read().decode("utf-8")
                return cuerpo if texto else json.loads(cuerpo)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404):  # error del cliente: reintentar no ayuda
                break
        except Exception as e:  # noqa: BLE001 - red, timeout, JSON truncado
            last = e
        time.sleep(2 ** i)
    raise RuntimeError(f"{url} -> {last}")


# ---------------------------------------------------------------- descargas (devuelven pd.Series indexada por fecha)
def _to_series(pairs):
    d = {pd.Timestamp(f): float(v) for f, v in pairs if v is not None}
    if not d:  # respuesta vacia: serie vacia pero con indice de fechas (falla luego por MIN_MESES, con mensaje claro)
        return pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    return pd.Series(d).sort_index()


def fetch_bcra(var_id, start, end):
    pairs, offset, total = [], 0, None
    for _ in range(60):  # tope de paginas: evita bucles si la API cambia
        j = http_json(f"{BCRA}/{var_id}", {"desde": start, "hasta": end, "limit": 1000, "offset": offset})
        res = j.get("results")
        det = (res[0].get("detalle") if res else j.get("detalle")) or []
        pairs += [(d["fecha"], d["valor"]) for d in det]
        total = (j.get("metadata", {}).get("resultset", {}) or {}).get("count", len(pairs))
        offset += len(det)
        if not det or offset >= total:
            break
    return _to_series(pairs)


def fetch_datos(series_id, start):
    j = http_json(DATOS, {"ids": series_id, "start_date": start[:7], "limit": 1000, "format": "json"})
    return _to_series([(r[0], r[1]) for r in j.get("data", [])])


def fetch_argdatos(kind, start):
    if kind in ("contadoconliqui", "bolsa", "blue"):
        rows = http_json(f"{ARGDATOS}/cotizaciones/dolares/{kind}")
        pairs = [(r["fecha"], r.get("venta")) for r in rows]
    else:  # riesgo pais
        rows = http_json(f"{ARGDATOS}/finanzas/indices/riesgo-pais")
        pairs = [(r["fecha"], r.get("valor")) for r in rows]
    s = _to_series(pairs)
    return s[s.index >= pd.Timestamp(start)]


def fetch_fred(series_id, start):
    """CSV abierto de FRED (sin clave). Primera columna = fecha, segunda = valor ('.' = sin dato)."""
    txt = http_json(FRED, {"id": series_id, "cosd": start}, texto=True)
    df = pd.read_csv(io.StringIO(txt))
    df = df.iloc[:, :2]
    df.columns = ["fecha", "valor"]
    df["valor"] = pd.to_numeric(df["valor"], errors="coerce")
    ser = _to_series(zip(df["fecha"], df["valor"].where(df["valor"].notna(), None)))
    if len(ser) < 2:
        return ser
    full = ser.asfreq("MS")  # meses que FRED no publico (p. ej. oct-2025, cierre del gobierno de EE.UU.) quedan NaN
    huecos = full[full.isna()].index
    if len(huecos):
        # interpolacion geometrica (tasa constante) solo entre dos datos reales y hasta 2 meses seguidos
        full = np.exp(np.log(full).interpolate(limit=2, limit_area="inside"))
        rellenados = [f"{t:%Y-%m}" for t in huecos if full.loc[t] == full.loc[t]]
        print(f"AVISO {series_id}: meses sin dato en FRED, interpolados: {', '.join(rellenados) or 'ninguno'}")
    return full.dropna()


def fetch_ipc_local(_id, _start):
    """Indice de precios de Argentina: encadena la variacion mensual de data/actuals_monthly.csv (serie IPC_MENSUAL, columna 'ultimo').
    Base ene-2016 = 100. Es un indice propio (empalme BCRA/INDEC): sirve para cocientes y variaciones, no como cifra oficial de nivel."""
    if not ACTUALS.exists():
        raise RuntimeError(f"falta {ACTUALS} (correr fetch_actuals.py antes)")
    a = pd.read_csv(ACTUALS)
    a = a[a["serie"] == "IPC_MENSUAL"].copy()
    if a.empty:
        raise RuntimeError("actuals_monthly.csv no tiene la serie IPC_MENSUAL")
    a["mes"] = pd.to_datetime(a["periodo"]).dt.to_period("M").dt.to_timestamp()
    v = a.drop_duplicates("mes", keep="last").set_index("mes")["ultimo"].astype(float).sort_index()
    v = v.asfreq("MS")
    if v.isna().any():  # un hueco rompe el encadenado: mejor fallar que inventar
        raise RuntimeError("IPC_MENSUAL con meses faltantes: " + ", ".join(f"{t:%Y-%m}" for t in v[v.isna()].index[:6]))
    idx = 100.0 * (1.0 + v / 100.0).cumprod()
    return idx[idx.index >= pd.Timestamp(_start)]


# ---------------------------------------------------------------- agregacion mensual
def to_monthly(s, agg, today):
    """Diario -> mensual (inicio de mes). Se descarta el mes en curso: un mes incompleto no es comparable."""
    s = s.sort_index()
    if agg != "none":
        g = s.resample("MS")
        s = {"last": g.last, "mean": g.mean}.get(agg, lambda: g.sum(min_count=1))()
    s = s.dropna()
    s.index = s.index.to_period("M").to_timestamp()  # inicio de mes (datos.gob.ar ya viene asi)
    return s[s.index < pd.Timestamp(today).to_period("M").to_timestamp()]


def brecha_mensual(ccl, tcm, today):
    d = pd.concat([ccl.rename("ccl"), tcm.rename("tcm")], axis=1, join="inner").dropna()  # solo dias con mayorista
    return to_monthly((d["ccl"] / d["tcm"] - 1.0) * 100.0, "mean", today)


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="lista de variables separadas por coma (depuracion: no escribe el CSV)")
    ap.add_argument("--today", default=str(date.today()), help="fecha de referencia YYYY-MM-DD (default: hoy)")
    ap.add_argument("--insecure", action="store_true", help="no verifica SSL, solo para api.bcra.gob.ar (igual que fetch_actuals.py)")
    args = ap.parse_args(argv)
    global INSECURE
    INSECURE = args.insecure
    today, end = args.today, args.today
    only = set(args.only.split(",")) if args.only else None

    cache = pd.read_csv(OUT, parse_dates=["fecha"]) if OUT.exists() else pd.DataFrame()
    got, daily = {}, {}
    fallos_oficiales, avisos = [], []

    for sp in SERIES:
        v = sp["var"]
        if only and v not in only and not any(b["var"] in only and b["base"] == v for b in BRECHAS + TCR) \
                and not (v == OFICIAL and any(b["var"] in only for b in BRECHAS + TCR)) \
                and not (v in ("IPC_EEUU", "IPC_ARG") and any(t["var"] in only for t in TCR)):
            continue
        try:
            if sp["src"] == "bcra":
                raw = fetch_bcra(sp["id"], START, end)
            elif sp["src"] == "datos":
                raw = fetch_datos(sp["id"], START)
            elif sp["src"] == "fred":
                raw = fetch_fred(sp["id"], START)
            elif sp["src"] == "local":
                raw = fetch_ipc_local(sp["id"], START)
            else:
                raw = fetch_argdatos(sp["id"], START)
            daily[v] = raw
            m = to_monthly(raw, sp["agg"], today)
            if len(m) < MIN_MESES:
                raise RuntimeError(f"solo {len(m)} meses con datos (minimo {MIN_MESES})")
            got[v] = m
            print(f"OK   {v:<22} {len(m):>3} meses  {m.index.min():%Y-%m} -> {m.index.max():%Y-%m}  ult={m.iloc[-1]:,.2f}")
        except Exception as e:  # noqa: BLE001
            if sp["src"] in BLANDAS or sp.get("soft"):
                prev = cache[cache["variable"] == v] if len(cache) else pd.DataFrame()
                if len(prev):
                    got[v] = prev.set_index("fecha")["valor"]
                    avisos.append(f"{v}: fallo ({e}); se conservan {len(prev)} filas previas")
                else:
                    avisos.append(f"{v}: fallo ({e}) y no hay datos previos; se omite")
            else:
                fallos_oficiales.append(f"{v}: {e}")

    # brechas contra el mayorista (si falla el dolar de terceros se conserva la brecha previa)
    for b in BRECHAS:
        bv = b["var"]
        if only and bv not in only:
            continue
        if b["base"] in daily and OFICIAL in daily:
            try:
                br = brecha_mensual(daily[b["base"]], daily[OFICIAL], today)
                if len(br) < MIN_MESES:
                    raise RuntimeError(f"solo {len(br)} meses")
                got[bv] = br
                print(f"OK   {bv:<22} {len(br):>3} meses  {br.index.min():%Y-%m} -> {br.index.max():%Y-%m}  ult={br.iloc[-1]:,.2f}")
            except Exception as e:  # noqa: BLE001
                avisos.append(f"{bv}: fallo ({e}); se omite")
        else:
            prev = cache[cache["variable"] == bv] if len(cache) else pd.DataFrame()
            if len(prev):
                got[bv] = prev.set_index("fecha")["valor"]
            avisos.append(f"{bv}: sin {b['base']} nuevo; " + ("se conserva la previa" if len(prev) else "se omite"))

    # tipo de cambio real bilateral (promedio del periodo comun = 100). Si falta algun insumo se conserva el valor previo.
    for t in TCR:
        tv = t["var"]
        if only and tv not in only:
            continue
        try:
            if not all(k in got for k in (t["base"], "IPC_EEUU", "IPC_ARG")):
                raise RuntimeError("falta algun insumo (" + ", ".join(k for k in (t["base"], "IPC_EEUU", "IPC_ARG") if k not in got) + ")")
            d = pd.concat([got[t["base"]].rename("d"), got["IPC_EEUU"].rename("us"), got["IPC_ARG"].rename("ar")],
                          axis=1, join="inner").dropna()
            if len(d) < MIN_MESES:
                raise RuntimeError(f"solo {len(d)} meses en comun")
            r = d["d"] * d["us"] / d["ar"]
            got[tv] = 100.0 * r / r.mean()
            print(f"OK   {tv:<22} {len(r):>3} meses  {r.index.min():%Y-%m} -> {r.index.max():%Y-%m}  ult={got[tv].iloc[-1]:,.1f} (prom.=100)")
        except Exception as e:  # noqa: BLE001
            prev = cache[cache["variable"] == tv] if len(cache) else pd.DataFrame()
            if len(prev):
                got[tv] = prev.set_index("fecha")["valor"]
            avisos.append(f"{tv}: {e}; " + ("se conserva la previa" if len(prev) else "se omite"))

    for a in avisos:
        print("AVISO", a)
    if fallos_oficiales:
        print("\nERROR: fallaron series oficiales; NO se escribe data/macro.csv:", file=sys.stderr)
        for f in fallos_oficiales:
            print("  -", f, file=sys.stderr)
        return 1
    if only:
        print("\n(--only: no se escribe el CSV)")
        return 0

    meta = {s["var"]: s for s in SERIES + BRECHAS + TCR}
    fuente = {"bcra": "BCRA", "datos": "datos.gob.ar (Balance Cambiario, BCRA)", "argdatos": "ArgentinaDatos (terceros)",
              "derivada": "Calculada: CCL (ArgentinaDatos) / mayorista (BCRA)",
              "fred": "FRED (St. Louis Fed)", "local": "BCRA (IPC mensual, encadenado)"}
    frames = []
    for v, s in got.items():
        d = pd.DataFrame({"fecha": pd.to_datetime(s.index), "variable": v, "valor": s.values})
        m = meta[v]
        d["etiqueta"], d["unidad"], d["fuente"] = m["etiqueta"], m["unidad"], m.get("fuente") or fuente[m["src"]]
        d["tipo"], d["agregacion"] = m["tipo"], m["agg"]
        frames.append(d)
    out = pd.concat(frames, ignore_index=True).sort_values(["variable", "fecha"])
    DATA.mkdir(exist_ok=True)
    out.to_csv(OUT, index=False)
    print(f"\nEscrito {OUT} ({len(out)} filas, {out['variable'].nunique()} variables, hasta {out['fecha'].max():%Y-%m})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
