"""
fetch_macro.py - Series macro-financieras para la pestaña «Contexto macro-financiero» de la app.

Genera data/macro.csv en formato largo (una fila por variable y mes):
    fecha (1er dia del mes), variable, valor, etiqueta, unidad, fuente, tipo, agregacion

Fuentes:
  * BCRA, API Estadisticas Monetarias v4.0 (series diarias -> se agregan a mes).
  * datos.gob.ar, API de Series de Tiempo (Balance Cambiario, ya mensual).
  * ArgentinaDatos (terceros, NO oficial): dolar CCL y riesgo pais. Si falla, se conserva lo ya guardado.

Politica de errores (pensada para el workflow mensual):
  * Si falla una serie OFICIAL (BCRA / datos.gob.ar): no se escribe nada y el script termina con codigo 1
    (el Action falla y no commitea datos a medias).
  * Si falla una serie de TERCEROS: se conservan las filas previas de data/macro.csv y se avisa por pantalla.

Solo usa libreria estandar + pandas. Uso:
    python fetch_macro.py                 # actualiza data/macro.csv
    python fetch_macro.py --only COMPRAS_BCRA,RESERVAS_BRUTAS   # depuracion (no escribe, solo muestra)
Variable de entorno opcional BCRA_CA_BUNDLE: ruta a un bundle de certificados si el entorno no valida la cadena
de la API del BCRA (no se desactiva nunca la verificacion TLS).
"""
import argparse
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

import pandas as pd

DATA = Path(__file__).parent / "data"
OUT = DATA / "macro.csv"
START = "2016-06-01"  # primer relevamiento del REM
UA = "rem-macro-fetch/1.0 (+https://github.com/mau1878/REM)"
BCRA = "https://api.bcra.gob.ar/estadisticas/v4.0/monetarias"
DATOS = "https://apis.datos.gob.ar/series/api/series/"
ARGDATOS = "https://api.argentinadatos.com/v1"
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
    # ---- terceros (no oficial)
    dict(var="DOLAR_CCL", src="argdatos", id="contadoconliqui", agg="mean", tipo="precio", unidad="ARS por USD",
         etiqueta="Dólar CCL (promedio del mes, precio de venta)"),
    dict(var="RIESGO_PAIS", src="argdatos", id="riesgo-pais", agg="last", tipo="indicador", unidad="pb",
         etiqueta="Riesgo país (fin de mes)"),
]
# derivada: brecha CCL / mayorista oficial (BCRA id 5), calculada dia a dia sobre dias habiles y luego promediada por mes
BRECHA = dict(var="BRECHA_CCL", agg="mean", tipo="indicador", unidad="%", src="derivada",
              etiqueta="Brecha entre dólar CCL y mayorista oficial (promedio del mes)")
TCM_ID = 5  # tipo de cambio mayorista de referencia (insumo de la brecha)


# ---------------------------------------------------------------- HTTP
INSECURE = False  # solo con --insecure; afecta unicamente a api.bcra.gob.ar (cadena SSL incompleta)


def http_json(url, params=None, retries=4, timeout=60):
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
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return json.loads(r.read().decode("utf-8"))
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
    if kind == "contadoconliqui":
        rows = http_json(f"{ARGDATOS}/cotizaciones/dolares/contadoconliqui")
        pairs = [(r["fecha"], r.get("venta")) for r in rows]
    else:  # riesgo pais
        rows = http_json(f"{ARGDATOS}/finanzas/indices/riesgo-pais")
        pairs = [(r["fecha"], r.get("valor")) for r in rows]
    s = _to_series(pairs)
    return s[s.index >= pd.Timestamp(start)]


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
        if only and v not in only and not (v in ("DOLAR_CCL",) and "BRECHA_CCL" in only):
            continue
        try:
            if sp["src"] == "bcra":
                raw = fetch_bcra(sp["id"], START, end)
            elif sp["src"] == "datos":
                raw = fetch_datos(sp["id"], START)
            else:
                raw = fetch_argdatos(sp["id"], START)
            daily[v] = raw
            m = to_monthly(raw, sp["agg"], today)
            if len(m) < MIN_MESES:
                raise RuntimeError(f"solo {len(m)} meses con datos (minimo {MIN_MESES})")
            got[v] = m
            print(f"OK   {v:<22} {len(m):>3} meses  {m.index.min():%Y-%m} -> {m.index.max():%Y-%m}  ult={m.iloc[-1]:,.2f}")
        except Exception as e:  # noqa: BLE001
            if sp["src"] == "argdatos":
                prev = cache[cache["variable"] == v] if len(cache) else pd.DataFrame()
                if len(prev):
                    got[v] = prev.set_index("fecha")["valor"]
                    avisos.append(f"{v}: fallo ({e}); se conservan {len(prev)} filas previas")
                else:
                    avisos.append(f"{v}: fallo ({e}) y no hay datos previos; se omite")
            else:
                fallos_oficiales.append(f"{v}: {e}")

    # brecha CCL / mayorista (necesita el mayorista diario; si falla el CCL se conserva la brecha previa)
    if not only or "BRECHA_CCL" in only:
        try:
            if "DOLAR_CCL" in daily:
                tcm = fetch_bcra(TCM_ID, START, end)
                b = brecha_mensual(daily["DOLAR_CCL"], tcm, today)
                if len(b) < MIN_MESES:
                    raise RuntimeError(f"solo {len(b)} meses")
                got["BRECHA_CCL"] = b
                print(f"OK   {'BRECHA_CCL':<22} {len(b):>3} meses  {b.index.min():%Y-%m} -> {b.index.max():%Y-%m}  ult={b.iloc[-1]:,.2f}")
            else:
                prev = cache[cache["variable"] == "BRECHA_CCL"] if len(cache) else pd.DataFrame()
                if len(prev):
                    got["BRECHA_CCL"] = prev.set_index("fecha")["valor"]
                avisos.append("BRECHA_CCL: sin CCL nuevo; " + ("se conserva la previa" if len(prev) else "se omite"))
        except Exception as e:  # noqa: BLE001  (el mayorista es del BCRA: si falla, es un fallo oficial)
            fallos_oficiales.append(f"BRECHA_CCL (mayorista id {TCM_ID}): {e}")

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

    meta = {s["var"]: s for s in SERIES + [BRECHA]}
    fuente = {"bcra": "BCRA", "datos": "datos.gob.ar (Balance Cambiario, BCRA)", "argdatos": "ArgentinaDatos (terceros)",
              "derivada": "Calculada: CCL (ArgentinaDatos) / mayorista (BCRA)"}
    frames = []
    for v, s in got.items():
        d = pd.DataFrame({"fecha": pd.to_datetime(s.index), "variable": v, "valor": s.values})
        m = meta[v]
        d["etiqueta"], d["unidad"], d["fuente"] = m["etiqueta"], m["unidad"], fuente[m["src"]]
        d["tipo"], d["agregacion"] = m["tipo"], m["agg"]
        frames.append(d)
    out = pd.concat(frames, ignore_index=True).sort_values(["variable", "fecha"])
    DATA.mkdir(exist_ok=True)
    out.to_csv(OUT, index=False)
    print(f"\nEscrito {OUT} ({len(out)} filas, {out['variable'].nunique()} variables, hasta {out['fecha'].max():%Y-%m})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
