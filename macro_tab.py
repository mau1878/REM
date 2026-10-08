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
