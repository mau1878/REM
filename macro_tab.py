"""
macro_tab.py - Pestaña «Contexto macro-financiero»: cruza el error del REM con series macro de data/macro.csv
(generado por fetch_macro.py). Es descriptivo: muestra, no prueba nada.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

MAC_C = "#F2A93B"  # color de la serie macro (el azul de la app es el REM)
REM_C = "#2F80ED"
TRANSF = {  # transformaciones con sentido segun el tipo de serie
    "flujo": ["Nivel", "Suma móvil de 3 meses"],
    "stock": ["Nivel", "Cambio mensual (%)", "Cambio interanual (%)"],
    "precio": ["Nivel", "Cambio mensual (%)", "Cambio interanual (%)"],
    "indicador": ["Nivel", "Cambio mensual (diferencia)"],
}
# Regimenes cambiarios (inicio y fin inclusive, por mes). Fechas de memoria: VERIFICAR y ajustar si hace falta.
# Libre: tras la salida del cepo de dic-2015. Cepo: restricciones reimpuestas el 1/9/2019 (Decreto 609/2019).
# Bandas: desde el 14/4/2025 (abr-2025 es un mes mixto: se lo asigna a Bandas).
REGIMENES = [("Libre", "2016-01-01", "2019-08-01"), ("Cepo", "2019-09-01", "2025-03-01"), ("Bandas", "2025-04-01", None)]
REG_COL = {"Libre": "rgba(46,160,67,0.12)", "Cepo": "rgba(214,69,65,0.12)", "Bandas": "rgba(47,128,237,0.12)"}
METRICAS = {"Error con signo (real − mediana)": "error", "Error absoluto": "abs_error"}


@st.cache_data(show_spinner=False)
def load_macro(path, mtime):  # mtime invalida el cache cuando se actualiza el CSV
    return pd.read_csv(path, parse_dates=["fecha"])


def transformar(s, modo):
    """s: serie mensual indexada por inicio de mes. Devuelve (serie transformada, unidad de la transformacion)."""
    s = s.sort_index().asfreq("MS")  # meses faltantes -> NaN (no se interpola)
    if modo == "Suma móvil de 3 meses":
        return s.rolling(3, min_periods=3).sum(), None
    if modo == "Cambio mensual (%)":
        return s.pct_change(1, fill_method=None) * 100, "%"
    if modo == "Cambio interanual (%)":
        return s.pct_change(12, fill_method=None) * 100, "%"
    if modo == "Cambio mensual (diferencia)":
        return s.diff(), "dif."
    return s, None


def serie_error(ERR, variable, unidad, h, col):
    e = ERR[(ERR["variable"] == variable) & (ERR["unidad_norm"] == unidad) & (ERR["periodo_tipo"] == "mes")
            & (ERR["horizonte_meses"] == h)]
    if "comparable" in e:
        e = e[e["comparable"]]
    mes = e["fecha_objetivo"].dt.to_period("M").dt.to_timestamp()
    return e.groupby(mes)[col].mean()


def render(ERR, data_dir, var_names, show, unit_label):
    path = Path(data_dir) / "macro.csv"
    st.caption("Cruza el error del REM (un punto por mes proyectado) con series macro-financieras. Esta pestaña tiene sus propios "
               "selectores: no depende de los filtros de la barra lateral. Solo usa períodos comparables.")
    if not path.exists():
        st.info("Falta data/macro.csv. Correr fetch_macro.py y commitear data/.")
        return
    macro = load_macro(str(path), path.stat().st_mtime)
    if macro.empty:
        st.warning("data/macro.csv está vacío.")
        return

    # ---- selectores
    base = ERR[ERR["periodo_tipo"] == "mes"]
    if "comparable" in base:
        base = base[base["comparable"]]
    pares = base[["variable", "unidad_norm"]].drop_duplicates()
    if pares.empty:
        st.info("No hay variables del REM con período mensual para cruzar.")
        return
    vars_ = sorted(pares["variable"].unique(), key=lambda v: var_names.get(v, v))
    c1, c2 = st.columns(2)
    v = c1.selectbox("Variable del REM", vars_, index=vars_.index("TC_NOMINAL") if "TC_NOMINAL" in vars_ else 0,
                     format_func=lambda x: var_names.get(x, x), key="mac_var")
    unis = pares[pares["variable"] == v]["unidad_norm"].tolist()
    u = unis[0] if len(unis) == 1 else c1.selectbox("Unidad", unis, key=f"mac_uni|{v}")
    hs = sorted(int(h) for h in base[(base["variable"] == v) & (base["unidad_norm"] == u)]["horizonte_meses"].dropna().unique())
    if not hs:
        st.info("Sin horizontes disponibles para esta variable.")
        return
    h = c1.select_slider("Horizonte del error (meses de anticipación)", hs, value=3 if 3 in hs else hs[len(hs) // 2],
                         key=f"mac_h|{v}|{u}")
    metrica = c1.radio("Medida del error", list(METRICAS), horizontal=True, key="mac_met")

    etiquetas = macro.drop_duplicates("variable").set_index("variable")["etiqueta"].to_dict()
    mvars = sorted(etiquetas, key=lambda x: etiquetas[x])
    mv = c2.selectbox("Serie macro-financiera", mvars, index=mvars.index("COMPRAS_BCRA") if "COMPRAS_BCRA" in mvars else 0,
                      format_func=lambda x: etiquetas[x], key="mac_serie")
    meta = macro[macro["variable"] == mv].iloc[-1]
    modo = c2.selectbox("Transformación", TRANSF.get(meta["tipo"], ["Nivel"]), key=f"mac_tr|{mv}")
    lag = c2.selectbox("Rezago de la serie macro (meses)", [0, 1, 2, 3], key="mac_lag",
                       help="0: se compara el error de un mes con la serie macro del mismo mes. 1: con la del mes anterior, etc.")

    # ---- datos cruzados
    s_mac, u_tr = transformar(macro[macro["variable"] == mv].set_index("fecha")["valor"], modo)
    e = serie_error(ERR, v, u, h, METRICAS[metrica])
    df = pd.concat([e.rename("err"), s_mac.shift(lag).rename("mac")], axis=1, join="inner").dropna()
    if df.empty:
        st.warning("No hay meses en común entre el error del REM y la serie macro para esta combinación.")
        return
    if len(df) > 1:
        meses =[f"{t:%Y-%m}" for t in df.index]
        d0, d1 = st.select_slider("Período (mes proyectado)", meses, value=(meses[0], meses[-1]), key=f"mac_rng|{v}|{u}|{h}|{mv}|{lag}")
        df = df[(df.index >= pd.Timestamp(d0 + "-01")) & (df.index <= pd.Timestamp(d1 + "-01"))]

    u_err = unit_label(u)
    u_mac = meta["unidad"] if u_tr is None else (u_tr if u_tr == "%" else f"{meta['unidad']} (dif.)")
    lag_txt = f" (macro rezagada {lag} m)" if lag else ""
    vname, mname = var_names.get(v, v), etiquetas[mv]
    if "ArgentinaDatos" in str(meta["fuente"]):
        st.caption(f"⚠️ **{mname}** viene de una fuente no oficial ({meta['fuente']}).")
    else:
        st.caption(f"Fuente de la serie macro: {meta['fuente']}.")

    # ---- métricas
    n = len(df)
    pear = df["err"].corr(df["mac"]) if n > 2 else np.nan
    # Spearman = Pearson sobre rangos (evita que pandas pida scipy, que no esta en requirements.txt)
    spear = df["err"].rank().corr(df["mac"].rank()) if n > 2 else np.nan
    m = st.columns(3)
    m[0].metric("Meses en la muestra (n)", f"{n:,}")
    m[1].metric("Correlación de Pearson", f"{pear:+.2f}" if pear == pear else "s/d",
                help="Asociación lineal entre el error y la serie macro (−1 a +1). No implica causalidad.")
    m[2].metric("Correlación de Spearman", f"{spear:+.2f}" if spear == spear else "s/d",
                help="Como la de Pearson pero sobre rangos: menos sensible a valores extremos.")
    if n < 24:
        st.warning(f"Solo {n} meses: con tan pocos datos cualquier correlación es muy inestable.")
    st.markdown('<div class="rem-callout">Lectura con cuidado: es una comparación descriptiva. Los meses consecutivos no son '
                'independientes (los errores y las series macro tienden a moverse en rachas), así que las correlaciones suelen verse más '
                'firmes de lo que son; con rezago 0 el error y la serie pueden estar respondiendo a un mismo episodio, no uno al otro. '
                'Dos o tres meses de crisis pueden dominar todo el resultado.</div>', unsafe_allow_html=True)

    # ---- gráfico 1: serie temporal con doble eje
    f1 = make_subplots(specs=[[{"secondary_y": True}]])
    f1.add_trace(go.Bar(x=df.index, y=df["err"], name=f"Error del REM a {h} meses", marker_color=REM_C, opacity=0.75), secondary_y=False)
    f1.add_trace(go.Scatter(x=df.index, y=df["mac"], mode="lines+markers", name=f"{mname}{lag_txt}", line=dict(color=MAC_C, width=2.5),
                            marker=dict(size=5)), secondary_y=True)
    f1.add_hline(y=0, line=dict(color="gray", width=1), secondary_y=False)
    f1.update_layout(title=f"{vname}: error del REM a {h} meses y {mname.split(' (')[0]}", height=470, hovermode="x unified",
                     legend=dict(orientation="h", y=-0.2), xaxis_title="Mes proyectado")
    f1.update_yaxes(title_text=f"error ({u_err})", secondary_y=False)
    f1.update_yaxes(title_text=u_mac, secondary_y=True, showgrid=False)
    show(f1)
    st.caption("Barras azules: error del REM (real − mediana proyectada; positivo = el REM se quedó corto). Línea naranja: la serie "
               "macro, con su propio eje a la derecha: los dos ejes tienen escalas distintas, no se pueden comparar a ojo.")

    # ---- gráfico 2: dispersión
    f2 = go.Figure(go.Scatter(
        x=df["mac"], y=df["err"], mode="markers", name="Meses",
        marker=dict(size=8, color=df.index.year, colorscale="Viridis", showscale=True, colorbar=dict(title="Año")),
        customdata=np.stack([df.index.strftime("%Y-%m")], axis=-1),
        hovertemplate="Mes %{customdata[0]}<br>Macro: %{x:,.2f}<br>Error: %{y:,.2f}<extra></extra>"))
    if n >= 3 and df["mac"].std() > 0:
        k, b0 = np.polyfit(df["mac"], df["err"], 1)
        xs = np.array([df["mac"].min(), df["mac"].max()])
        f2.add_trace(go.Scatter(x=xs, y=k * xs + b0, mode="lines", name="Recta de ajuste lineal",
                                line=dict(color="gray", dash="dash"), hoverinfo="skip"))
    f2.update_layout(title=f"{vname}: error del REM a {h} meses vs. {mname.split(' (')[0]}{lag_txt}", height=430,
                     xaxis_title=u_mac, yaxis_title=f"error ({u_err})", legend=dict(orientation="h", y=-0.2))
    show(f2)

    # ---- tabla y descarga
    with st.expander("Datos cruzados y descripción de las series"):
        t = df.rename(columns={"err": f"error ({u_err})", "mac": f"{mname} [{u_mac}]"}).copy()
        t.index = [f"{x:%Y-%m}" for x in t.index]
        t.index.name = "mes proyectado"
        st.dataframe(t.round(3), width="stretch")
        st.download_button("Descargar datos cruzados (CSV)", t.to_csv().encode("utf-8"), file_name=f"rem_vs_{mv}.csv", mime="text/csv")
        ult = macro.sort_values("fecha").groupby("variable").tail(1).copy()
        ult["último mes"] = ult["fecha"].dt.strftime("%Y-%m")
        st.dataframe(ult[["variable", "etiqueta", "unidad", "fuente", "agregacion", "último mes"]].reset_index(drop=True),
                     width="stretch", hide_index=True)
        st.caption("Las series diarias se pasan a mensual: «last» = último dato del mes, «sum» = suma del mes, «mean» = promedio; "
                   "«none» = ya es mensual. Se descarta el mes en curso. Saldos y flujos en USD mn / ARS mn son nominales.")


def regimen_de(ts):
    for nombre, a, b in REGIMENES:
        if ts >= pd.Timestamp(a) and (b is None or ts <= pd.Timestamp(b)):
            return nombre
    return "Previo"


def _sombrear(fig, x0, x1):
    """Sombrea los regimenes cambiarios que caen dentro de [x0, x1] (meses, inicio de mes)."""
    for nombre, a, b in REGIMENES:
        ini = max(pd.Timestamp(a), x0)
        fin = min((pd.Timestamp(b) + pd.offsets.MonthBegin(1)) if b else x1 + pd.offsets.MonthBegin(1), x1 + pd.offsets.MonthBegin(1))
        if ini < fin:
            fig.add_vrect(x0=ini, x1=fin, fillcolor=REG_COL[nombre], line_width=0, layer="below",
                          annotation_text=nombre, annotation_position="top left", annotation_font_size=11)


def _corr_row(d, x, y):
    n = len(d)
    p = d[x].corr(d[y]) if n > 2 else np.nan
    sp = d[x].rank().corr(d[y].rank()) if n > 2 else np.nan
    return n, p, sp


def _unidad(meta, u_tr):
    return meta["unidad"] if u_tr is None else (u_tr if u_tr == "%" else f"{meta['unidad']} (dif.)")


def render_cruce(data_dir, show):
    """Pestaña «Cruce de series macro»: dos series de macro.csv entre sí (sin el error del REM)."""
    path = Path(data_dir) / "macro.csv"
    st.caption("Cruza dos series macro-financieras entre sí. No usa el REM. Tiene sus propios selectores: no depende de los filtros "
               "de la barra lateral.")
    if not path.exists():
        st.info("Falta data/macro.csv. Correr fetch_macro.py y commitear data/.")
        return
    macro = load_macro(str(path), path.stat().st_mtime)
    if macro.empty or macro["variable"].nunique() < 2:
        st.warning("data/macro.csv no tiene al menos dos series para cruzar.")
        return
    et = macro.drop_duplicates("variable").set_index("variable")["etiqueta"].to_dict()
    mvars = sorted(et, key=lambda x: et[x])

    def _idx(k, alt):
        return mvars.index(k) if k in mvars else alt

    ca, cb = st.columns(2)
    a = ca.selectbox("Serie A", mvars, index=_idx("RESERVAS_BRUTAS", 0), format_func=lambda x: et[x], key="crx_a")
    b = cb.selectbox("Serie B", mvars, index=_idx("DOLAR_CCL", min(1, len(mvars) - 1)), format_func=lambda x: et[x], key="crx_b")
    ma = macro[macro["variable"] == a].iloc[-1]
    mb = macro[macro["variable"] == b].iloc[-1]
    ta = ca.selectbox("Transformación de A", TRANSF.get(ma["tipo"], ["Nivel"]), index=min(1, len(TRANSF.get(ma["tipo"], ["Nivel"])) - 1),
                      key=f"crx_ta|{a}")
    tb = cb.selectbox("Transformación de B", TRANSF.get(mb["tipo"], ["Nivel"]), index=min(1, len(TRANSF.get(mb["tipo"], ["Nivel"])) - 1),
                      key=f"crx_tb|{b}")
    k = st.select_slider("Desfase de B respecto de A (meses)", list(range(-6, 13)), value=0, key="crx_k",
                         help="k > 0: se compara A de un mes con B de k meses después (A «adelanta» a B). k < 0: al revés.")
    if a == b and k == 0 and ta == tb:
        st.warning("Elegiste la misma serie con la misma transformación y sin desfase: la correlación es trivialmente 1.")

    sa, ua = transformar(macro[macro["variable"] == a].set_index("fecha")["valor"], ta)
    sb, ub = transformar(macro[macro["variable"] == b].set_index("fecha")["valor"], tb)
    df = pd.concat([sa.rename("A"), sb.shift(-k).rename("B")], axis=1, join="inner").dropna()
    if df.empty:
        st.warning("No hay meses en común entre las dos series con esta combinación.")
        return
    if len(df) > 1:
        meses = [f"{t:%Y-%m}" for t in df.index]
        d0, d1 = st.select_slider("Período (mes de la serie A)", meses, value=(meses[0], meses[-1]), key=f"crx_rng|{a}|{b}|{k}")
        df = df[(df.index >= pd.Timestamp(d0 + "-01")) & (df.index <= pd.Timestamp(d1 + "-01"))]

    reg = st.radio("Régimen cambiario", ["Todo"] + [r[0] for r in REGIMENES], horizontal=True, key="crx_reg",
                   help="Filtra los meses por régimen (fechas en REGIMENES, macro_tab.py). Las correlaciones cambian mucho de un régimen a otro.")
    por_reg = []
    for nombre, *_ in REGIMENES:
        sub = df[[regimen_de(t) == nombre for t in df.index]]
        n_, p_, s_ = _corr_row(sub, "A", "B")
        por_reg.append({"Régimen": nombre, "n": n_, "Pearson": None if p_ != p_ else round(p_, 2), "Spearman": None if s_ != s_ else round(s_, 2)})
    if reg != "Todo":
        df = df[[regimen_de(t) == reg for t in df.index]]
        if df.empty:
            st.warning("No hay meses de este régimen en el período elegido.")
            return
    na, nb = et[a].split(" (")[0], et[b].split(" (")[0]
    uA, uB = _unidad(ma, ua), _unidad(mb, ub)
    ktxt = f" (B desplazada {k:+d} m)" if k else ""
    for m_ in (ma, mb):
        if "ArgentinaDatos" in str(m_["fuente"]):
            st.caption(f"⚠️ **{et[m_['variable']]}** viene de una fuente no oficial ({m_['fuente']}).")

    n = len(df)
    pear = df["A"].corr(df["B"]) if n > 2 else np.nan
    spear = df["A"].rank().corr(df["B"].rank()) if n > 2 else np.nan
    m = st.columns(3)
    m[0].metric("Meses en la muestra (n)", f"{n:,}")
    m[1].metric("Correlación de Pearson", f"{pear:+.2f}" if pear == pear else "s/d")
    m[2].metric("Correlación de Spearman", f"{spear:+.2f}" if spear == spear else "s/d")
    if n < 24:
        st.warning(f"Solo {n} meses: con tan pocos datos cualquier correlación es muy inestable.")
    tendencia = {"stock", "precio"}
    if ta == "Nivel" and tb == "Nivel" and ma["tipo"] in tendencia and mb["tipo"] in tendencia:
        st.warning("Ambas series están en nivel y suelen tener tendencia (o inflación nominal de por medio): dos series que suben "
                   "con el tiempo se correlacionan fuerte aunque no tengan relación. Probá con cambio mensual o interanual.")
    with st.expander("Correlación por régimen cambiario (período elegido)"):
        st.dataframe(pd.DataFrame(por_reg), hide_index=True, width="stretch")
        st.caption("Los regímenes con pocos meses (en especial Bandas) dan correlaciones muy inestables.")
    st.markdown('<div class="rem-callout">Lectura con cuidado: es una comparación descriptiva. Los meses consecutivos no son '
                'independientes, así que las correlaciones suelen verse más firmes de lo que son, y unos pocos meses de crisis '
                'pueden dominar el resultado. Que dos series se muevan juntas no dice cuál causa a cuál, ni si hay una tercera '
                'detrás.</div>', unsafe_allow_html=True)

    f1 = make_subplots(specs=[[{"secondary_y": True}]])
    f1.add_trace(go.Scatter(x=df.index, y=df["A"], mode="lines", name=f"A: {na} · {ta}", line=dict(color=REM_C, width=2.5)),
                 secondary_y=False)
    f1.add_trace(go.Scatter(x=df.index, y=df["B"], mode="lines", name=f"B: {nb} · {tb}{ktxt}", line=dict(color=MAC_C, width=2.5)),
                 secondary_y=True)
    _sombrear(f1, df.index.min(), df.index.max())
    f1.update_layout(title=f"{na} y {nb}", height=470, hovermode="x unified", legend=dict(orientation="h", y=-0.2),
                     xaxis_title="Mes de la serie A")
    f1.update_yaxes(title_text=uA, secondary_y=False)
    f1.update_yaxes(title_text=uB, secondary_y=True, showgrid=False)
    show(f1)
    st.caption("Cada serie tiene su propio eje: las escalas son distintas y no se pueden comparar a ojo.")

    f2 = go.Figure(go.Scatter(
        x=df["A"], y=df["B"], mode="markers", name="Meses",
        marker=dict(size=8, color=df.index.year, colorscale="Viridis", showscale=True, colorbar=dict(title="Año")),
        customdata=np.stack([df.index.strftime("%Y-%m")], axis=-1),
        hovertemplate="Mes %{customdata[0]}<br>A: %{x:,.2f}<br>B: %{y:,.2f}<extra></extra>"))
    if n >= 3 and df["A"].std() > 0:
        kk, b0 = np.polyfit(df["A"], df["B"], 1)
        xs = np.array([df["A"].min(), df["A"].max()])
        f2.add_trace(go.Scatter(x=xs, y=kk * xs + b0, mode="lines", name="Recta de ajuste lineal",
                                line=dict(color="gray", dash="dash"), hoverinfo="skip"))
    f2.update_layout(title=f"{nb}{ktxt} vs. {na}", height=430, xaxis_title=f"A: {na} [{uA}]", yaxis_title=f"B: {nb} [{uB}]",
                     legend=dict(orientation="h", y=-0.2))
    show(f2)

    if n >= 18:
        rc = df["A"].rolling(12, min_periods=12).corr(df["B"]).dropna()
        f3 = go.Figure(go.Scatter(x=rc.index, y=rc, mode="lines", line=dict(color=MAC_C, width=2.5), name="Pearson móvil 12 m"))
        f3.add_hline(y=0, line=dict(color="gray", width=1))
        f3.update_layout(title="Correlación de Pearson en ventana móvil de 12 meses", height=330, yaxis=dict(range=[-1, 1]),
                         xaxis_title="Último mes de la ventana", yaxis_title="Pearson")
        show(f3)
        st.caption("Si la correlación cambia de signo o de magnitud según el período, la relación no es estable.")

    with st.expander("Datos cruzados y descripción de las series"):
        t = df.rename(columns={"A": f"A: {na} [{uA}]", "B": f"B: {nb} [{uB}]"}).copy()
        t.index = [f"{x:%Y-%m}" for x in t.index]
        t.index.name = "mes (serie A)"
        st.dataframe(t.round(3), width="stretch")
        st.download_button("Descargar datos cruzados (CSV)", t.to_csv().encode("utf-8"), file_name=f"{a}_vs_{b}.csv", mime="text/csv")
        ult = macro[macro["variable"].isin([a, b])].sort_values("fecha").groupby("variable").tail(1).copy()
        ult["último mes"] = ult["fecha"].dt.strftime("%Y-%m")
        st.dataframe(ult[["variable", "etiqueta", "unidad", "fuente", "agregacion", "último mes"]].reset_index(drop=True),
                     width="stretch", hide_index=True)


# ====================================================================== Termómetro de presión cambiaria
# Cada componente: (variable, transformacion, signo). signo = +1 si MÁS valor => MÁS presión; -1 si MÁS valor => MENOS presión.
# Los signos se definen a priori (no se estiman con el dólar). El dólar NO entra al índice: se usa solo para validarlo.
COMPONENTES = {
    "COMPRAS_BCRA": ("Nivel", -1, "Compras de divisas del BCRA"),
    "RESERVAS_BRUTAS": ("Cambio mensual (%)", -1, "Reservas internacionales"),
    "DEPOSITOS_USD_PRIV": ("Cambio mensual (%)", -1, "Depósitos en USD del sector privado"),
    "CC_BIENES": ("Nivel", -1, "Cuenta corriente cambiaria: bienes"),
    "CC_SERVICIOS": ("Nivel", -1, "Cuenta corriente cambiaria: servicios"),
    "FAE_PRIV_NETA": ("Nivel", -1, "Formación de activos externos del sector privado (neta)"),
    "RIESGO_PAIS": ("Cambio mensual (diferencia)", +1, "Riesgo país (fuente no oficial)"),
}
DEFAULT_ON = ["COMPRAS_BCRA", "RESERVAS_BRUTAS", "DEPOSITOS_USD_PRIV", "CC_BIENES", "CC_SERVICIOS", "FAE_PRIV_NETA"]
Z_CLIP = 3.0
MIN_Z = 12  # meses minimos de historia para estandarizar un componente


def _modo(etiqueta):
    """Etiqueta del radio de estandarización -> modo interno."""
    if etiqueta.startswith("Móvil"):
        return "Móvil de 36 meses"
    if etiqueta.startswith("Dentro"):
        return "Por régimen"
    return "Expansiva"


def _zscore(x, modo):
    """z-score sin mirar el futuro (usa la historia hasta el mes t inclusive).
    «Expansiva»: toda la historia disponible. «Móvil de 36 meses»: ventana móvil. «Por régimen»: solo los meses ya transcurridos
    del mismo régimen cambiario (evita confundir el nivel propio de cada régimen con presión); hasta tener MIN_Z meses del
    régimen se usa la expansiva."""
    x = x.sort_index()
    if modo == "Móvil de 36 meses":
        mu, sd = x.rolling(36, min_periods=MIN_Z).mean(), x.rolling(36, min_periods=MIN_Z).std()
        return ((x - mu) / sd.replace(0, np.nan)).clip(-Z_CLIP, Z_CLIP)
    mu, sd = x.expanding(MIN_Z).mean(), x.expanding(MIN_Z).std()
    z = (x - mu) / sd.replace(0, np.nan)
    if modo == "Por régimen":
        reg = pd.Series([regimen_de(t) for t in x.index], index=x.index)
        zr = pd.Series(np.nan, index=x.index)
        for nombre in reg.unique():
            seg = x[reg == nombre]
            zr.loc[seg.index] = (seg - seg.expanding(MIN_Z).mean()) / seg.expanding(MIN_Z).std().replace(0, np.nan)
        z = zr.fillna(z)
    return z.clip(-Z_CLIP, Z_CLIP)


def construir_indice(macro, vars_, modo):
    """Devuelve (indice, aportes, panel_z). aportes: contribucion de cada componente al indice (signo aplicado, dividido por la cantidad
    de componentes disponibles ese mes). Se exige al menos la mitad de los componentes para calcular el mes."""
    cols = {}
    for v in vars_:
        modo_tr, signo, _ = COMPONENTES[v]
        s, _ = transformar(macro[macro["variable"] == v].set_index("fecha")["valor"], modo_tr)
        cols[v] = _zscore(s, modo) * signo
    panel = pd.DataFrame(cols)
    disp = panel.notna().sum(axis=1)
    ok = disp >= max(1, int(np.ceil(len(vars_) / 2)))
    aportes = panel.div(disp.replace(0, np.nan), axis=0).where(ok, np.nan)
    idx = panel.mean(axis=1).where(ok)
    return idx.dropna(), aportes.loc[idx.dropna().index], panel


def pca_primer_componente(panel):
    """Primer componente principal del panel (filas completas). Signo ajustado para correlacionar + con el promedio."""
    d = panel.dropna()
    if len(d) < 24 or d.shape[1] < 2:
        return None, None, None
    z = (d - d.mean()) / d.std().replace(0, np.nan)
    z = z.dropna(axis=1)
    if z.shape[1] < 2:
        return None, None, None
    w, V = np.linalg.eigh(np.corrcoef(z.values.T))
    v = V[:, -1]
    sc = pd.Series(z.values @ v, index=z.index)
    if sc.corr(d.mean(axis=1)) < 0:
        v, sc = -v, -sc
    return sc, pd.Series(v, index=z.columns), float(w[-1] / w.sum())


def oos_expansivo(x, y, h, min_train=36, delta=False):
    """Pronostico fuera de muestra de y(t+h) con regresion lineal de y(t+h) sobre x(t), reestimada cada mes con datos conocidos a t.
    delta=True: se pronostica el cambio y(t+h) - y(t) (util para niveles persistentes como la brecha, donde «el promedio
    historico» seria un benchmark muy flojo). Benchmark: promedio historico del objetivo. Devuelve DataFrame con error del modelo y del benchmark por mes de pronostico."""
    d = pd.concat([x.rename("x"), y.rename("y")], axis=1).asfreq("MS")
    xs, y0 = d["x"].values, d["y"].values
    # objetivo asociado al mes t: z[t] = y(t+h) (o y(t+h) - y(t) si delta)
    zs = np.full(len(d), np.nan)
    if len(d) > h:
        zs[:len(d) - h] = y0[h:] - (y0[:len(d) - h] if delta else 0.0)
    filas = []
    for t in range(len(d)):
        tgt = t + h
        if tgt >= len(d) or np.isnan(xs[t]) or np.isnan(zs[t]):
            continue
        idx = [s for s in range(t - h + 1) if not (np.isnan(xs[s]) or np.isnan(zs[s]))]  # pares (x_s, z_s) con s + h <= t: ya conocidos
        if len(idx) < min_train or np.std(xs[idx]) == 0:
            continue
        k, b0 = np.polyfit(xs[idx], zs[idx], 1)
        bench = float(np.mean(zs[idx]))
        filas.append((d.index[tgt], (k * xs[t] + b0) - zs[t], bench - zs[t]))
    return pd.DataFrame(filas, columns=["mes", "e_mod", "e_bench"]).set_index("mes")


# Variables que se comparan una a una en el estudio de eventos: los componentes del índice y candidatos «de mercado».
# (variable, transformacion, signo [+1: más valor = más presión], etiqueta)
CANDIDATOS = [(v, tr, sg, lab) for v, (tr, sg, lab) in COMPONENTES.items()] + [
    ("RIESGO_PAIS", "Nivel", +1, "Riesgo país: nivel (fuente no oficial)"),
    ("BRECHA_CCL", "Nivel", +1, "Brecha CCL: nivel (incluye una cotización del dólar)"),
    ("BRECHA_MEP", "Nivel", +1, "Brecha MEP: nivel (incluye una cotización del dólar)"),
]


def por_componente(macro, ev, modo_z, items, desde=-3, hasta=-1):
    """Para cada item: z-score (signo aplicado, sin mirar el futuro) promedio en e+desde..e+hasta sobre los eventos, contra el promedio de
    todos los meses. Devuelve un DataFrame con una fila por item."""
    filas = []
    for v, tr, sg, lab in items:
        if v not in set(macro["variable"]):
            continue
        x, _ = transformar(macro[macro["variable"] == v].set_index("fecha")["valor"], tr)
        z = (_zscore(x, modo_z) * sg).dropna()
        if len(z) < 24:
            continue
        W = ventana_evento(z, ev, desde, 0)
        pre = W[list(range(desde, hasta + 1))]
        un_mes = W[-1].dropna()
        n = len(un_mes)
        base = float(z.mean())
        t_aprox = (un_mes.mean() - base) / (z.std() / np.sqrt(n)) if n >= 3 and z.std() > 0 else np.nan
        fila = {"Variable": lab, "Eventos con dato": n, "Promedio 1 mes antes": un_mes.mean() if n else np.nan,
                "Promedio 1 a 3 meses antes": np.nanmean(pre.values) if pre.notna().any().any() else np.nan,
                "Promedio de todos los meses": base, "% eventos > 0 (1 mes antes)": (un_mes > 0).mean() * 100 if n else np.nan,
                "% de todos los meses > 0": (z > 0).mean() * 100, "t aprox.": t_aprox}
        for j in range(desde, hasta + 1):
            fila[f"m{j}"] = W[j].mean()
        filas.append(fila)
    return pd.DataFrame(filas)


def eventos_saltos(jump, umbral, sep=3):
    """Meses con salto >= umbral. Eventos a menos de `sep` meses del anterior se consideran el mismo episodio (se usa el primero)."""
    ev = []
    for t in jump.index[jump >= umbral]:
        if not ev or (t.year - ev[-1].year) * 12 + (t.month - ev[-1].month) >= sep:
            ev.append(t)
    return ev


def ventana_evento(idx, ev, desde=-6, hasta=3):
    """Matriz eventos x meses relativos con el valor del indice en e+j (NaN si no hay dato)."""
    m = {}
    for e in ev:
        m[e] = [idx.get(e + pd.offsets.MonthBegin(j) if j >= 0 else e - pd.offsets.MonthBegin(-j), np.nan) for j in range(desde, hasta + 1)]
    return pd.DataFrame(m, index=range(desde, hasta + 1)).T


def render_termometro(data_dir, show):
    path = Path(data_dir) / "macro.csv"
    st.caption("Índice compuesto de presión cambiaria hecho con series macro (no incluye ninguna cotización del dólar). Se valida "
               "contra el dólar que elijas. Es descriptivo: mide presión, no predice el dólar.")
    if not path.exists():
        st.info("Falta data/macro.csv. Correr fetch_macro.py y commitear data/.")
        return
    macro = load_macro(str(path), path.stat().st_mtime)
    disp = [v for v in COMPONENTES if v in set(macro["variable"])]
    if len(disp) < 2:
        st.warning("data/macro.csv no tiene suficientes series para armar el índice.")
        return

    c1, c2 = st.columns(2)
    sel = c1.multiselect("Componentes del índice", disp, default=[v for v in DEFAULT_ON if v in disp], key="ter_comp",
                         format_func=lambda v: COMPONENTES[v][2])
    modo_z = c2.radio("Estandarización", ["Dentro del régimen", "Expansiva (solo pasado)", "Móvil de 36 meses"], key="ter_z", horizontal=True,
                      help="Cada mes se estandariza con la historia disponible hasta ese mes, para no usar información del futuro. «Dentro del "
                           "régimen» compara cada mes solo con meses anteriores del mismo régimen cambiario (hasta tener 12 meses del régimen "
                           "usa la expansiva).")
    if len(sel) < 2:
        st.info("Elegí al menos dos componentes.")
        return
    with st.expander("Signos y transformaciones de cada componente"):
        st.dataframe(pd.DataFrame([{"Componente": COMPONENTES[v][2], "Transformación": COMPONENTES[v][0],
                                    "Más valor significa": "MÁS presión" if COMPONENTES[v][1] > 0 else "MENOS presión"}
                                   for v in sel]), hide_index=True, width="stretch")
        st.caption(f"Cada componente se estandariza (z-score, recortado a ±{Z_CLIP:g}) y se promedia con su signo. El índice es cero en "
                   "la «normalidad histórica» de cada serie hasta ese mes: positivo = más presión que lo habitual, negativo = menos. "
                   "Los signos son una decisión a priori, no se estiman con el dólar.")
    idx_all, aportes, panel = construir_indice(macro, sel, _modo(modo_z))
    # meses finales con componentes que faltan (p. ej. el Balance Cambiario publica con un mes de rezago): se muestran como
    # provisorios y NO entran en las pruebas (correlación, fuera de muestra, eventos), para no comparar un mes incompleto con completos
    completo = panel.reindex(idx_all.index).notna().sum(axis=1) == len(sel)
    ult_completo = completo[completo].index.max() if completo.any() else None
    idx = idx_all[idx_all.index <= ult_completo] if ult_completo is not None else idx_all.iloc[0:0]
    prov = idx_all[idx_all.index > ult_completo] if ult_completo is not None else idx_all
    if len(idx) < 24:
        st.warning("Muy pocos meses para evaluar el índice.")
        return
    if len(prov):
        st.caption(f"⚠️ {', '.join(f'{t:%Y-%m}' for t in prov.index)}: provisorio (faltan componentes que se publican con rezago). "
                   "Se muestra en los gráficos, pero no entra en las pruebas.")

    informe = []  # secciones de texto para el archivo descargable (para poder leerlo/compartirlo sin capturas)

    def _sec(titulo, obj=None):
        informe.append(f"## {titulo}\n" + ("" if obj is None else (obj if isinstance(obj, str) else obj.to_csv(index=not isinstance(obj.index, pd.RangeIndex)))))

    # ---- dólar de validación
    vals = macro.drop_duplicates("variable").set_index("variable")
    objetivos = [v for v in vals.index if v.startswith("DOLAR_") or v.startswith("BRECHA_")]
    d1, d2, d3 = st.columns(3)
    obj = d1.selectbox("Dólar para validar", objetivos, index=objetivos.index("DOLAR_CCL") if "DOLAR_CCL" in objetivos else 0,
                       format_func=lambda v: vals.loc[v, "etiqueta"].split(" (")[0], key="ter_obj")
    tr_opts = TRANSF.get(vals.loc[obj, "tipo"], ["Nivel"])
    tr_obj = d2.selectbox("Transformación", tr_opts, index=min(1, len(tr_opts) - 1), key=f"ter_tr|{obj}")
    reg = d3.radio("Régimen", ["Todo"] + [r[0] for r in REGIMENES], key="ter_reg", horizontal=False)
    y, u_y = transformar(macro[macro["variable"] == obj].set_index("fecha")["valor"], tr_obj)
    u_ylab = _unidad(vals.loc[obj], u_y)
    _sec("Parametros", f"dolar_validacion={obj} ({vals.loc[obj, 'etiqueta']}); transformacion={tr_obj}; regimen_filtro_correlacion={reg}\n"
                       f"componentes={', '.join(sel)}; estandarizacion={modo_z}; ultimo_mes_macro={macro['fecha'].max():%Y-%m}; "
                       f"meses_indice={len(idx)} ({idx.index.min():%Y-%m} a {idx.index.max():%Y-%m}); provisorios_excluidos={','.join(f'{t:%Y-%m}' for t in prov.index) or 'ninguno'}\n"
                       "regimenes=" + "; ".join(f"{n_}: {a_[:7]} a {(b_ or 'hoy')[:7]}" for n_, a_, b_ in REGIMENES) + "\n")
    if "ArgentinaDatos" in str(vals.loc[obj, "fuente"]):
        st.caption(f"⚠️ **{vals.loc[obj, 'etiqueta']}** viene de una fuente no oficial ({vals.loc[obj, 'fuente']}).")

    # ---- gráfico 1: índice + dólar
    f1 = make_subplots(specs=[[{"secondary_y": True}]])
    f1.add_trace(go.Scatter(x=idx.index, y=idx, mode="lines", name="Índice de presión", line=dict(color=REM_C, width=2.8)), secondary_y=False)
    yy = y.reindex(idx.index)
    f1.add_trace(go.Scatter(x=yy.index, y=yy, mode="lines", name=f"{vals.loc[obj, 'etiqueta'].split(' (')[0]} · {tr_obj}",
                            line=dict(color=MAC_C, width=1.8), opacity=0.9), secondary_y=True)
    if len(prov):
        f1.add_trace(go.Scatter(x=prov.index, y=prov, mode="markers", name="Provisorio (faltan componentes)",
                                marker=dict(color=REM_C, size=9, symbol="circle-open", line=dict(width=2))), secondary_y=False)
    f1.add_hline(y=0, line=dict(color="gray", width=1), secondary_y=False)
    _sombrear(f1, idx_all.index.min(), idx_all.index.max())
    f1.update_layout(title="Índice de presión cambiaria y dólar", height=480, hovermode="x unified", legend=dict(orientation="h", y=-0.2))
    f1.update_yaxes(title_text="índice (desvíos estándar)", secondary_y=False)
    f1.update_yaxes(title_text=u_ylab, secondary_y=True, showgrid=False)
    show(f1)
    st.caption("Sombreado: régimen cambiario. Cada línea tiene su eje: las escalas no son comparables a ojo.")

    # ---- gráfico 2: aportes
    f2 = go.Figure()
    for v in aportes.columns:
        f2.add_trace(go.Bar(x=aportes.index, y=aportes[v], name=COMPONENTES[v][2]))
    f2.add_trace(go.Scatter(x=idx_all.index, y=idx_all, mode="lines", name="Índice", line=dict(color="black", width=1.8)))
    f2.update_layout(title="Aporte de cada componente al índice", barmode="relative", height=430, hovermode="x unified",
                     legend=dict(orientation="h", y=-0.3), yaxis_title="desvíos estándar")
    show(f2)

    # ---- correlaciones cruzadas
    st.subheader("¿Anticipa o acompaña al dólar?")
    base = pd.concat([idx.rename("idx"), y.rename("y")], axis=1).asfreq("MS")
    lags = list(range(-6, 7))
    filas = []
    for k in lags:
        d = pd.concat([base["idx"], base["y"].shift(-k)], axis=1).dropna()
        d.columns = ["idx", "y"]
        if reg != "Todo":
            d = d[[regimen_de(t) == reg for t in d.index]]
        n_, p_, s_ = _corr_row(d, "idx", "y")
        filas.append((k, n_, p_, s_))
    cc = pd.DataFrame(filas, columns=["k", "n", "pearson", "spearman"])
    f3 = go.Figure(go.Bar(x=cc["k"], y=cc["pearson"], marker_color=[MAC_C if k == 0 else REM_C for k in cc["k"]],
                           customdata=cc[["n", "spearman"]].values,
                           hovertemplate="k=%{x}<br>Pearson %{y:+.2f}<br>Spearman %{customdata[1]:+.2f}<br>n=%{customdata[0]}<extra></extra>"))
    f3.add_hline(y=0, line=dict(color="gray", width=1))
    f3.update_layout(title=f"Correlación entre el índice del mes t y el dólar del mes t+k (régimen: {reg})", height=380,
                     xaxis=dict(title="k (meses). k > 0: el índice antecede al dólar; k < 0: lo sigue", dtick=1), yaxis_title="Pearson")
    show(f3)
    _sec(f"Correlacion cruzada indice(t) vs dolar(t+k), regimen={reg}", cc.round(4))
    n0 = int(cc.loc[cc["k"] == 0, "n"].iloc[0])
    if n0 < 24:
        st.warning(f"Solo {n0} meses en este régimen: las correlaciones son muy inestables.")
    st.caption("Con varias combinaciones de rezagos y regímenes siempre aparece alguna barra «alta» por azar: lo que cuenta es un patrón "
               "coherente y estable entre regímenes, no la barra más alta.")

    # ---- fuera de muestra
    st.subheader("Prueba fuera de muestra")
    h = st.select_slider("Horizonte (meses hacia adelante)", [1, 2, 3, 6], value=1, key="ter_h")
    delta = tr_obj == "Nivel"
    oo = oos_expansivo(idx, y, h, delta=delta)
    if delta:
        st.caption("Como el dólar elegido está en nivel (serie persistente), se pronostica el **cambio** entre el mes t y t+h, y el "
                   "benchmark es el cambio promedio histórico. Así no se premia al índice por la persistencia de la serie.")
    if len(oo) < 12:
        st.info("No hay meses suficientes para la prueba fuera de muestra (se necesitan 36 de entrenamiento más al menos 12 de evaluación).")
    else:
        r2 = 1 - (oo["e_mod"] ** 2).sum() / (oo["e_bench"] ** 2).sum()
        m = st.columns(3)
        _sec(f"Fuera de muestra (h={h}, objetivo={'cambio' if delta else 'valor'} en t+h)", f"r2_oos={r2:.4f}; meses={len(oo)}; "
             f"primer_mes={oo.index.min():%Y-%m}\n")
        m[0].metric("R² fuera de muestra", f"{r2:+.1%}", help="Positivo: el índice mejora el pronóstico respecto de usar el promedio histórico. "
                                                              "Negativo o cercano a cero: no agrega nada.")
        m[1].metric("Meses evaluados", f"{len(oo):,}")
        m[2].metric("Primer mes evaluado", f"{oo.index.min():%Y-%m}")
        f4 = go.Figure(go.Scatter(x=oo.index, y=((oo["e_bench"] ** 2) - (oo["e_mod"] ** 2)).cumsum(), mode="lines", line=dict(color=REM_C, width=2.5)))
        f4.add_hline(y=0, line=dict(color="gray", width=1))
        _sombrear(f4, oo.index.min(), oo.index.max())
        f4.update_layout(title="Ventaja acumulada del índice sobre el promedio histórico (suma de errores² evitados)", height=360,
                         yaxis_title="acumulado (unidades²)")
        show(f4)
        st.caption("Si la línea sube de a saltos en pocos meses, la ventaja la explican uno o dos episodios; si sube de forma pareja, es más "
                   "estable. Si no está claramente por encima de cero, el índice no mejoró el pronóstico.")
        tab = []
        for nombre, *_ in REGIMENES:
            sub = oo[[regimen_de(t) == nombre for t in oo.index]]
            if len(sub):
                tab.append({"Régimen (mes pronosticado)": nombre, "Meses": len(sub),
                            "R² fuera de muestra": f"{1 - (sub['e_mod'] ** 2).sum() / (sub['e_bench'] ** 2).sum():+.1%}"})
        if tab:
            st.dataframe(pd.DataFrame(tab), hide_index=True, width="stretch")
            _sec("Fuera de muestra por regimen", pd.DataFrame(tab))

    # ---- estudio de eventos
    st.subheader("Estudio de eventos: ¿qué marcaba el índice antes de los saltos?")
    raw_obj = macro[macro["variable"] == obj].set_index("fecha")["valor"].sort_index().asfreq("MS")
    es_pct = vals.loc[obj, "tipo"] == "precio"
    jump = (raw_obj.pct_change(1, fill_method=None) * 100) if es_pct else raw_obj.diff()
    jump = jump.dropna()
    uj = "%" if es_pct else "pp"
    um = st.slider(f"Umbral del salto mensual ({uj})", 5.0, 40.0, 15.0 if es_pct else 10.0, 1.0, key=f"ter_um|{obj}",
                   help="Un evento es un mes en que el dólar (o la brecha) sube al menos este valor. Los meses seguidos (a menos de 3 de "
                        "distancia) cuentan como un solo episodio.")
    ev = [e for e in eventos_saltos(jump, um) if e in idx.index or (e - pd.offsets.MonthBegin(1)) in idx.index]
    if len(ev) < 3:
        st.info(f"Solo {len(ev)} eventos con salto ≥ {um:g} {uj}: bajá el umbral para tener algo que mirar.")
    else:
        W = ventana_evento(idx, ev)
        base_media = float(idx.mean())
        f5 = go.Figure()
        for e in W.index:
            f5.add_trace(go.Scatter(x=W.columns, y=W.loc[e], mode="lines", line=dict(color="rgba(150,150,150,0.45)", width=1),
                                    name=f"{e:%Y-%m}", hovertemplate=f"{e:%Y-%m}" + "<br>mes %{x}: %{y:+.2f}<extra></extra>", showlegend=False))
        f5.add_trace(go.Scatter(x=W.columns, y=W.mean(), mode="lines+markers", line=dict(color=MAC_C, width=3.2), name="Promedio de los eventos"))
        f5.add_trace(go.Scatter(x=W.columns, y=W.median(), mode="lines", line=dict(color=REM_C, width=2.2, dash="dot"), name="Mediana"))
        f5.add_hline(y=base_media, line=dict(color="gray", dash="dash", width=1.5),
                     annotation_text="promedio de todos los meses", annotation_position="bottom right")
        f5.add_vline(x=0, line=dict(color="gray", width=1))
        f5.update_layout(title=f"Índice de presión alrededor de {len(ev)} saltos de al menos {um:g} {uj}", height=430,
                         xaxis=dict(title="Meses respecto del salto (0 = mes del salto)", dtick=1), yaxis_title="índice (desvíos estándar)",
                         legend=dict(orientation="h", y=-0.25))
        show(f5)
        pre = W[[-3, -2, -1]]
        pos = (idx > 0).mean()
        c = st.columns(3)
        c[0].metric("Índice promedio 1 mes antes", f"{W[-1].mean():+.2f}", help=f"Promedio de todos los meses: {base_media:+.2f}")
        c[1].metric("Índice promedio 1 a 3 meses antes", f"{np.nanmean(pre.values):+.2f}")
        c[2].metric("Eventos con índice > 0 un mes antes", f"{(W[-1] > 0).sum()} de {int(W[-1].notna().sum())}",
                    help=f"En el {pos:.0%} de todos los meses el índice es positivo: esa es la referencia, no 50%.")
        _sec(f"Estudio de eventos: umbral={um:g} {uj}, eventos={len(ev)}; indice promedio/mediana por mes relativo (0 = mes del salto); "
             f"promedio de todos los meses={base_media:.3f}",
             pd.DataFrame({"mes_relativo": W.columns, "promedio": W.mean().round(3).values, "mediana": W.median().round(3).values,
                           "n": W.notna().sum().values}))
        tabla = pd.DataFrame({"Mes del salto": [f"{e:%Y-%m}" for e in W.index], f"Salto ({uj})": [round(float(jump[e]), 1) for e in W.index],
                              "Régimen": [regimen_de(e) for e in W.index],
                              "Índice 1 mes antes": W[-1].round(2).values, "Índice 3 meses antes": W[-3].round(2).values})
        st.dataframe(tabla, hide_index=True, width="stretch")
        _sec("Eventos (mes, salto, regimen, indice 1 y 3 meses antes)", tabla)
        st.caption("Si el índice sirviera de alerta, las líneas deberían estar claramente por encima del promedio de todos los meses "
                   "(línea punteada) *antes* del mes 0. Con pocos eventos, un par de casos pueden mover todo el promedio: mirá la tabla y "
                   "las líneas individuales (grises), no solo el promedio.")

        st.markdown("**Una variable por vez:** ¿alguna de las series que componen el índice (o un candidato de mercado) marcaba presión "
                    "antes de los saltos? Cada serie se mide en desvíos estándar con el signo «más valor = más presión» (positivo = más presión "
                    "que lo habitual).")
        pc = por_componente(macro, ev, _modo(modo_z), CANDIDATOS)
        if pc.empty:
            st.info("No hay series suficientes para esta comparación.")
        else:
            f6 = go.Figure()
            for j, col in ((-3, "#9bbcf2"), (-2, "#5b94ec"), (-1, REM_C)):
                f6.add_trace(go.Bar(x=pc["Variable"], y=pc[f"m{j}"], name=f"{-j} mes{'es' if j < -1 else ''} antes", marker_color=col))
            f6.add_hline(y=0, line=dict(color="gray", width=1))
            f6.update_layout(title=f"Presión media de cada serie antes de los {len(ev)} saltos", barmode="group", height=470,
                             yaxis_title="desvíos estándar (signo: más = más presión)", legend=dict(orientation="h", y=-0.55),
                             xaxis=dict(tickangle=-35))
            show(f6)
            tb = pc[["Variable", "Eventos con dato", "Promedio 1 mes antes", "Promedio 1 a 3 meses antes", "Promedio de todos los meses",
                     "% eventos > 0 (1 mes antes)", "% de todos los meses > 0", "t aprox."]].copy()
            for c_ in tb.columns[2:]:
                tb[c_] = tb[c_].round(2)
            st.dataframe(tb, hide_index=True, width="stretch")
            _sec("Una variable por vez (z-score con signo; m-3..m-1 = promedio de eventos 3..1 meses antes)", pc.round(3))
            st.caption("«t aprox.» compara el promedio de 1 mes antes con el promedio de todos los meses, usando la dispersión de la serie: "
                       "valores por encima de ±2 serían llamativos, pero con 8 a 10 eventos y 9 series probadas siempre puede aparecer una "
                       "por azar, y los meses vecinos no son independientes. Tomalo como pista para mirar, no como prueba. Las brechas "
                       "incluyen una cotización del dólar: si «anticipan», en parte es persistencia del propio dólar.")

    # ---- control con PCA y descarga
    with st.expander("Control: primer componente principal (PCA) y datos"):
        sc, carga, share = pca_primer_componente(panel)
        if sc is None:
            st.write("No hay suficientes meses completos para calcular el componente principal.")
        else:
            cr = sc.corr(idx.reindex(sc.index))
            st.write(f"El primer componente explica **{share:.0%}** de la varianza del panel y su correlación con el índice transparente es "
                     f"**{cr:+.2f}**. Si es baja, el promedio con signos a priori no está capturando el factor común de los datos. "
                     "(El PCA usa toda la muestra: es solo un control, no un indicador utilizable en tiempo real.)")
            st.dataframe(pd.DataFrame({"Componente": [COMPONENTES[v][2] for v in carga.index], "Peso": carga.round(3).values}),
                         hide_index=True, width="stretch")
        t = pd.concat([idx_all.rename("indice")], axis=1).join(aportes.add_prefix("aporte_"))
        t["provisorio"] = [int(x in prov.index) for x in t.index]
        t.index = [f"{x:%Y-%m}" for x in t.index]
        st.download_button("Descargar índice y aportes (CSV)", t.round(4).to_csv().encode("utf-8"), file_name="termometro_presion.csv", mime="text/csv")

    st.download_button("Descargar resumen del termómetro (TXT, para compartir con Claude)", ("\n".join(informe)).encode("utf-8"),
                       file_name="termometro_resumen.txt", mime="text/plain", key="ter_dl",
                       help="Parámetros, correlación cruzada, prueba fuera de muestra y estudio de eventos de lo que estás viendo, en texto plano.")


# ====================================================================== Tensión de mercado (panel de alerta)
SENALES = {  # clave: (variable, transformacion, signo, etiqueta)
    "RIESGO_PAIS|Nivel": ("RIESGO_PAIS", "Nivel", +1, "Riesgo país (nivel)"),
    "BRECHA_CCL|Nivel": ("BRECHA_CCL", "Nivel", +1, "Brecha CCL (nivel)"),
    "BRECHA_MEP|Nivel": ("BRECHA_MEP", "Nivel", +1, "Brecha MEP (nivel)"),
    "BRECHA_BLUE|Nivel": ("BRECHA_BLUE", "Nivel", +1, "Brecha blue (nivel)"),
    "RIESGO_PAIS|Cambio mensual (diferencia)": ("RIESGO_PAIS", "Cambio mensual (diferencia)", +1, "Riesgo país (cambio mensual)"),
}
SENALES_DEFAULT = ["RIESGO_PAIS|Nivel", "BRECHA_CCL|Nivel"]


def senal_z(macro, clave, modo):
    v, tr, sg, _ = SENALES[clave]
    x, _ = transformar(macro[macro["variable"] == v].set_index("fecha")["valor"], tr)
    return (_zscore(x, modo) * sg).dropna()


def evaluar_alerta(z, jump, th, h, cut):
    """Alerta en el mes t si z(t) > cut. Resultado a acertar: salto >= th en alguno de los meses t+1..t+h.
    Se evalúa solo donde hay datos de los h meses siguientes. Devuelve un dict de métricas y la serie de aciertos."""
    jump = jump.sort_index()
    z = z.reindex(jump.index)
    y = pd.Series(np.nan, index=jump.index)
    vals = jump.values
    for i in range(len(vals) - h):
        w = vals[i + 1:i + 1 + h]
        y.iloc[i] = float(np.any(w >= th)) if not np.all(np.isnan(w)) else np.nan
    d = pd.concat([z.rename("z"), y.rename("y")], axis=1).dropna()
    al = d[d["z"] > cut]
    sin = d[d["z"] <= cut]
    ev = eventos_saltos(jump.dropna(), th)
    cap = 0
    elig = 0
    for e in ev:
        pre = [e - pd.offsets.MonthBegin(j) for j in range(1, h + 1)]
        zs = [z.get(t, np.nan) for t in pre]
        if np.all(np.isnan(zs)):
            continue
        elig += 1
        cap += int(np.nanmax(zs) > cut)
    pos = d["y"].sum()
    return {
        "n": len(d), "tasa_base": d["y"].mean() if len(d) else np.nan,
        "alertas": len(al), "aciertos": int(al["y"].sum()), "falsas": int(len(al) - al["y"].sum()),
        "precision": al["y"].mean() if len(al) else np.nan, "sin_alerta": sin["y"].mean() if len(sin) else np.nan,
        "recall": (al["y"].sum() / pos) if pos else np.nan, "meses_previos": int(pos),
        "episodios": elig, "episodios_con_alerta": cap, "d": d,
    }


def render_tension(data_dir, show):
    path = Path(data_dir) / "macro.csv"
    st.caption("Panel de alerta con variables de mercado (riesgo país y brechas). Mide qué tan seguido, tras una señal de tensión, hubo un salto "
               "del dólar en los meses siguientes. Es una prueba histórica y descriptiva: no es un pronóstico.")
    if not path.exists():
        st.info("Falta data/macro.csv. Correr fetch_macro.py y commitear data/.")
        return
    macro = load_macro(str(path), path.stat().st_mtime)
    have = set(macro["variable"])
    claves = [k for k, v in SENALES.items() if v[0] in have]
    dolares = [v for v in ("DOLAR_CCL", "DOLAR_MEP", "DOLAR_BLUE", "DOLAR_OFICIAL") if v in have]
    if not claves or not dolares:
        st.warning("data/macro.csv no tiene riesgo país, brechas o cotizaciones del dólar suficientes.")
        return
    et = macro.drop_duplicates("variable").set_index("variable")["etiqueta"].to_dict()
    c1, c2, c3 = st.columns(3)
    sel = c1.multiselect("Señales", claves, default=[k for k in SENALES_DEFAULT if k in claves], key="ten_sel",
                         format_func=lambda k: SENALES[k][3])
    tgt = c2.selectbox("Dólar cuyo salto se quiere anticipar", dolares, key="ten_tgt", format_func=lambda v: et[v].split(" (")[0])
    th = c3.slider("Salto mensual (≥ %)", 5, 40, 10, 1, key="ten_th", help="Variación del promedio mensual del dólar respecto del mes anterior.")
    c4, c5, c6 = st.columns(3)
    h = c4.slider("Ventana de aviso (meses)", 1, 6, 3, 1, key="ten_h", help="Una alerta «acierta» si hay un salto en alguno de los h meses siguientes.")
    cut = c5.slider("Umbral de alerta (z >)", 0.0, 2.0, 1.0, 0.25, key="ten_cut",
                    help="Desvíos estándar sobre lo habitual (calculado solo con datos anteriores) a partir de los cuales hay alerta.")
    modo_z = c6.radio("Estandarización", ["Expansiva (solo pasado)", "Móvil de 36 meses"], key="ten_z", help="Cada mes se compara solo con datos anteriores.")
    if not sel:
        st.info("Elegí al menos una señal.")
        return
    modo = _modo(modo_z)
    zs = {SENALES[k][3]: senal_z(macro, k, modo) for k in sel}
    if len(sel) >= 2:
        zs["Combinada (promedio)"] = pd.concat(list(zs.values()), axis=1).mean(axis=1).dropna()
    raw = macro[macro["variable"] == tgt].set_index("fecha")["valor"].sort_index().asfreq("MS")
    jump = (raw.pct_change(1, fill_method=None) * 100)
    if tgt == "DOLAR_CCL" and any("CCL" in k for k in sel) or tgt == "DOLAR_MEP" and any("MEP" in k for k in sel) \
            or tgt == "DOLAR_BLUE" and any("BLUE" in k for k in sel):
        st.caption("⚠️ La brecha elegida incluye la propia cotización del dólar que querés anticipar: parte de la «señal» es persistencia del precio.")
    if "ArgentinaDatos" in str(macro[macro["variable"] == tgt]["fuente"].iloc[-1]):
        st.caption(f"⚠️ **{et[tgt].split(' (')[0]}** y las brechas con dólares libres vienen de ArgentinaDatos (fuente no oficial).")

    res = {n: evaluar_alerta(z, jump, th, h, cut) for n, z in zs.items()}
    ev = eventos_saltos(jump.dropna(), th)
    informe = [f"## Parametros\ndolar={tgt}; salto>={th}%; ventana={h} meses; alerta si z>{cut}; estandarizacion={modo_z}; "
               f"senales={', '.join(sel)}; ultimo_mes={jump.dropna().index.max():%Y-%m}; episodios={len(ev)}\n"]
    base_ref = next(iter(res.values()))["tasa_base"]

    # ---- lectura actual
    st.subheader("Lectura actual")
    cols = st.columns(len(zs))
    filas_act = []
    for col, (n, z) in zip(cols, zs.items()):
        ult = z.index.max()
        val = float(z.iloc[-1])
        col.metric(n, f"{val:+.2f}", delta="EN ALERTA" if val > cut else "fuera de alerta", delta_color="inverse" if val > cut else "off",
                   help=f"Último dato: {ult:%Y-%m}. Desvíos estándar sobre lo habitual con datos anteriores.")
        filas_act.append({"senal": n, "mes": f"{ult:%Y-%m}", "z": round(val, 3), "en_alerta": bool(val > cut)})
    informe.append("## Lectura actual\n" + pd.DataFrame(filas_act).to_csv(index=False))

    # ---- gráfico
    f1 = go.Figure()
    colores = {0: REM_C, 1: MAC_C, 2: "#8e6bd1", 3: "#2ea043", 4: "#d64541"}
    for i, (n, z) in enumerate(zs.items()):
        f1.add_trace(go.Scatter(x=z.index, y=z, mode="lines", name=n, line=dict(color=colores.get(i, "gray"), width=3 if n.startswith("Comb") else 1.8,
                                                                               dash="dot" if n.startswith("Comb") else "solid")))
    f1.add_hline(y=cut, line=dict(color="gray", dash="dash", width=1.5), annotation_text="umbral de alerta", annotation_position="top left")
    for e in ev:
        f1.add_vline(x=e, line=dict(color="rgba(214,69,65,0.55)", width=1.5))
    allz = pd.concat(list(zs.values()))
    _sombrear(f1, allz.index.min(), allz.index.max())
    f1.update_layout(title=f"Señales de tensión y {len(ev)} saltos de al menos {th}% del dólar (líneas rojas)", height=470, hovermode="x unified",
                     legend=dict(orientation="h", y=-0.2), yaxis_title="desvíos estándar sobre lo habitual")
    show(f1)

    # ---- tabla de aciertos
    st.subheader("¿Cuánto acertó cada señal?")
    fil = []
    for n, r in res.items():
        fil.append({"Señal": n, "Meses de alerta": r["alertas"], "Seguidos de salto": r["aciertos"], "Falsas alarmas": r["falsas"],
                    "% de alertas con salto": None if r["precision"] != r["precision"] else round(r["precision"] * 100),
                    "% sin alerta con salto": None if r["sin_alerta"] != r["sin_alerta"] else round(r["sin_alerta"] * 100),
                    "Tasa base %": None if r["tasa_base"] != r["tasa_base"] else round(r["tasa_base"] * 100),
                    "% meses previos a salto con alerta": None if r["recall"] != r["recall"] else round(r["recall"] * 100),
                    "Episodios con alerta previa": f"{r['episodios_con_alerta']} de {r['episodios']}"})
    tb = pd.DataFrame(fil)
    st.dataframe(tb, hide_index=True, width="stretch")
    informe.append("## Aciertos por senal\n" + tb.to_csv(index=False))
    st.caption(f"Meses evaluados: {next(iter(res.values()))['n']} (los que tienen datos de los {h} meses siguientes). «Tasa base» es el % de meses "
               f"con un salto en la ventana, haya o no alerta. Una señal sirve si «% de alertas con salto» supera claramente a «% sin alerta con "
               f"salto» y a la tasa base, **sin** generar demasiadas falsas alarmas.")

    # ---- por régimen
    reg_f = []
    for n, r in res.items():
        d = r["d"]
        for nombre, *_ in REGIMENES:
            sub = d[[regimen_de(t) == nombre for t in d.index]]
            al = sub[sub["z"] > cut]
            if len(sub):
                reg_f.append({"Señal": n, "Régimen": nombre, "Meses": len(sub), "Alertas": len(al), "Seguidas de salto": int(al["y"].sum()),
                              "Meses con salto en ventana": int(sub["y"].sum())})
    with st.expander("Desglose por régimen cambiario"):
        rg = pd.DataFrame(reg_f)
        st.dataframe(rg, hide_index=True, width="stretch")
        st.caption("Con pocos meses o saltos en un régimen (en especial Bandas), las cuentas no permiten sacar conclusiones.")
    informe.append("## Por regimen\n" + pd.DataFrame(reg_f).to_csv(index=False))
    informe.append("## Episodios (primer mes de cada salto)\n" + "\n".join(f"{e:%Y-%m},{jump[e]:.1f}%,{regimen_de(e)}" for e in ev) + "\n")

    st.markdown('<div class="rem-callout">Lectura con cuidado: el umbral y la ventana se pueden mover hasta que el resultado se vea bien, y eso '
                'infla los aciertos. Los meses seguidos de una misma crisis cuentan varias veces, pero hay pocos episodios independientes '
                '(mirá «Episodios con alerta previa»). Si una señal acierta la mitad de las veces, la otra mitad son falsas alarmas. '
                'Es una verificación histórica sobre datos ya conocidos, no una garantía hacia adelante.</div>', unsafe_allow_html=True)
    st.download_button("Descargar resumen de tensión (TXT, para compartir con Claude)", "\n".join(informe).encode("utf-8"),
                       file_name="tension_resumen.txt", mime="text/plain", key="ten_dl")


# ====================================================================== Dólar y tipo de cambio real
TCR_NECESARIAS = ["TCR_OFICIAL", "TCR_CCL", "IPC_ARG", "IPC_EEUU", "DOLAR_OFICIAL", "DOLAR_CCL"]


def _pct_hist(s, x):
    """Porcentaje de meses de s con valor <= x (posicion de x dentro de la historia)."""
    return float((s <= x).mean() * 100) if len(s) else np.nan


def render_tcr(data_dir, show):
    """Pestaña «Dólar y tipo de cambio real»: qué tan caro/barato está el dólar descontando inflación y qué dólar nominal
    sería compatible con distintos niveles de referencia. Descriptivo: no pronostica nada."""
    path = Path(data_dir) / "macro.csv"
    st.caption("¿Qué tan caro o barato está el dólar descontando inflación, y qué dólar nominal sería compatible con distintos niveles "
               "de referencia? Es un ejercicio de aritmética con la historia, no un pronóstico. No usa el REM ni los filtros de la barra lateral.")
    if not path.exists():
        st.info("Falta data/macro.csv. Correr fetch_macro.py y commitear data/.")
        return
    macro = load_macro(str(path), path.stat().st_mtime)
    faltan = [v for v in TCR_NECESARIAS if v not in set(macro["variable"])]
    if faltan:
        st.info("Faltan series en data/macro.csv: " + ", ".join(faltan) + ". Correr la Action de ingesta (fetch_macro.py nuevo) y commitear data/.")
        return

    def ser(v):
        return macro[macro["variable"] == v].set_index("fecha")["valor"].sort_index()

    modo = st.radio("Dólar de referencia", ["Mayorista oficial", "CCL"], horizontal=True, key="tcr_dolar",
                    help="Bajo cepo el oficial era un precio administrado: para 2019-2025 es más informativo el CCL. Con bandas, ambos están cerca.")
    tv, dv = ("TCR_OFICIAL", "DOLAR_OFICIAL") if modo == "Mayorista oficial" else ("TCR_CCL", "DOLAR_CCL")
    tcr, dol = ser(tv), ser(dv)
    d = pd.concat([tcr.rename("tcr"), dol.rename("dol")], axis=1, join="inner").dropna()
    if len(d) < 24:
        st.warning("Hay menos de 24 meses con todos los insumos: no alcanza para comparar con la historia.")
        return
    base = d.index.max()
    t0, d0 = float(d.loc[base, "tcr"]), float(d.loc[base, "dol"])
    reg = pd.Series([regimen_de(t) for t in d.index], index=d.index)

    c = st.columns(4)
    c[0].metric(f"Tipo de cambio real ({base:%Y-%m})", f"{t0:.1f}", delta=f"{(t0 / 100 - 1) * 100:+.1f}% vs. promedio 2016–hoy (=100)", delta_color="off",
                help="Índice: dólar × IPC EE.UU. ÷ IPC Argentina, reescalado para que el promedio de todo el período valga 100. Más alto = dólar más caro en términos reales.")
    c[1].metric("Posición en la historia", f"{_pct_hist(d['tcr'], t0):.0f}%", help="Porcentaje de meses de la historia con un tipo de cambio real igual o menor al actual.")
    c[2].metric("Mínimo / máximo histórico", f"{d['tcr'].min():.0f} / {d['tcr'].max():.0f}")
    c[3].metric(f"Dólar nominal ({base:%Y-%m})", f"{d0:,.0f}", help="Promedio del mes, en pesos.")
    st.caption("Último mes con todos los insumos (dólar, IPC Argentina e IPC EE.UU.). El IPC se publica con rezago, por eso puede ser anterior al último dato de dólar.")

    # --- gráfico 1: tipo de cambio real por régimen
    f1 = go.Figure(go.Scatter(x=d.index, y=d["tcr"], mode="lines", name="Tipo de cambio real", line=dict(color=MAC_C, width=2.5)))
    f1.add_hline(y=100, line=dict(color="gray", width=1, dash="dot"), annotation_text="Promedio 2016–hoy", annotation_position="bottom right")
    for nombre, _, _ in REGIMENES:
        sub = d.loc[reg == nombre, "tcr"]
        if len(sub) >= 3:
            f1.add_trace(go.Scatter(x=[sub.index.min(), sub.index.max()], y=[sub.median()] * 2, mode="lines", name=f"Mediana {nombre}",
                                    line=dict(color="rgba(80,80,80,0.8)", width=1.5, dash="dash"), hovertemplate=f"Mediana {nombre}: %{{y:.1f}}<extra></extra>"))
    _sombrear(f1, d.index.min(), d.index.max())
    f1.update_layout(title=f"Tipo de cambio real bilateral con EE.UU. ({modo}), promedio del período = 100", height=430, hovermode="x unified",
                     legend=dict(orientation="h", y=-0.2), yaxis_title="Índice", xaxis_title="Mes")
    show(f1)

    # --- gráfico 2: dolar nominal vs el dolar que mantendria el TCR en su promedio
    ref = d["dol"] * 100.0 / d["tcr"]
    f2 = go.Figure()
    f2.add_trace(go.Scatter(x=d.index, y=d["dol"], mode="lines", name="Dólar nominal", line=dict(color=REM_C, width=2.5)))
    f2.add_trace(go.Scatter(x=ref.index, y=ref, mode="lines", name="Dólar con tipo de cambio real = promedio (100)",
                            line=dict(color=MAC_C, width=2, dash="dash")))
    _sombrear(f2, d.index.min(), d.index.max())
    f2.update_layout(title="Dólar nominal y dólar que mantendría constante el tipo de cambio real", height=400, hovermode="x unified",
                     legend=dict(orientation="h", y=-0.2), yaxis=dict(type="log", title="Pesos por USD (escala logarítmica)"), xaxis_title="Mes")
    show(f2)
    st.caption("La línea punteada es la inflación relativa Argentina/EE.UU. acumulada, anclada al nivel promedio de 2016–hoy. Cuando el dólar nominal "
               "queda por encima, está caro en términos reales; por debajo, barato. Es una referencia mecánica, no un «dólar de equilibrio».")

    # --- tabla por regimen
    filas = []
    for nombre, _, _ in REGIMENES:
        sub = d.loc[reg == nombre, "tcr"]
        if len(sub):
            filas.append({"Régimen": nombre, "Meses": len(sub), "Mínimo": round(sub.min(), 1), "Mediana": round(sub.median(), 1),
                          "Máximo": round(sub.max(), 1), "Último": round(sub.iloc[-1], 1)})
    st.subheader("Tipo de cambio real por régimen")
    st.dataframe(pd.DataFrame(filas), hide_index=True, width="stretch")
    if modo == "Mayorista oficial":
        st.caption("Con el oficial, Cepo refleja un precio administrado. Para ese período conviene cambiar a CCL.")
    else:
        st.caption("El CCL de Cepo incluye brechas de 50% a 150%: por eso su promedio está inflado y el nivel actual se ve bajo en comparación.")

    # --- calculadora
    st.subheader("¿Qué dólar nominal sería compatible con cada referencia?")
    ipc_ar, ipc_us = ser("IPC_ARG"), ser("IPC_EEUU")
    pi_ar0 = float(ipc_ar.pct_change().tail(3).mean() * 100)
    pi_us0 = float(ipc_us.pct_change().tail(12).mean() * 100)
    k = st.columns(3)
    n_m = k[0].slider("Horizonte (meses desde el último dato)", 0, 24, 12, key="tcr_n")
    p_ar = k[1].number_input("Inflación mensual Argentina (%)", value=round(pi_ar0, 2), step=0.1, min_value=-1.0, max_value=50.0, key="tcr_piar",
                             help="Por defecto, el promedio de los últimos 3 meses de la serie de IPC. Es un supuesto tuyo: cámbialo.")
    p_us = k[2].number_input("Inflación mensual EE.UU. (%)", value=round(pi_us0, 2), step=0.05, min_value=-1.0, max_value=5.0, key="tcr_pius",
                             help="Por defecto, el promedio de los últimos 12 meses.")
    custom = st.number_input("Nivel personalizado del tipo de cambio real (100 = promedio 2016–hoy)", value=100.0, step=1.0, min_value=10.0, max_value=400.0, key="tcr_custom")
    factor = ((1 + p_ar / 100) / (1 + p_us / 100)) ** n_m

    refs = [("Hoy (mismo tipo de cambio real)", t0), ("Promedio 2016–hoy", 100.0)]
    for nombre, _, _ in REGIMENES:
        sub = d.loc[reg == nombre, "tcr"]
        if len(sub) >= 12:
            refs.append((f"Mediana {nombre}", float(sub.median())))
    bandas = d.loc[reg == "Bandas", "tcr"]
    if len(bandas) >= 6:
        refs.append(("Mínimo Bandas", float(bandas.min())))
        refs.append(("Máximo Bandas", float(bandas.max())))
    refs.append(("Personalizado", float(custom)))
    out = []
    for nombre, r_ in refs:
        dn = d0 * (r_ / t0) * factor
        out.append({"Referencia": nombre, "Tipo de cambio real": round(r_, 1), "Variación real vs. hoy (%)": round((r_ / t0 - 1) * 100, 1),
                    "Dólar nominal compatible": round(dn), "Variación nominal vs. hoy (%)": round((dn / d0 - 1) * 100, 1)})
    st.dataframe(pd.DataFrame(out), hide_index=True, width="stretch")
    st.caption(f"Cuenta: dólar de hoy × (referencia ÷ tipo de cambio real de hoy) × ((1 + inflación Argentina) ÷ (1 + inflación EE.UU.))^{n_m} meses. "
               f"Con {p_ar:.2f}% y {p_us:.2f}% mensual, la inflación relativa suma {(factor - 1) * 100:+.1f}% en {n_m} meses. Base: {base:%Y-%m}.")

    st.markdown('<div class="rem-callout">Cómo leerlo: no dice dónde <b>va</b> el dólar sino dónde <b>estaría</b> si el tipo de cambio real volviera a '
                'cada nivel de referencia. Límites: (1) es bilateral con EE.UU., no incluye Brasil ni China; (2) el promedio 2016–hoy incluye años de cepo y '
                'crisis, no es un equilibrio; (3) mayores exportaciones de energía y minería podrían sostener un tipo de cambio real más bajo que el '
                'histórico, pero estos datos no permiten estimar cuánto; (4) el IPC de Argentina es un índice encadenado desde variaciones mensuales; '
                '(5) con pocos meses en Bandas, sus medianas y extremos son poco confiables.</div>', unsafe_allow_html=True)
