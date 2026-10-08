"""Convierte el Excel «Deuda de la Administración Central» (Secretaría de Finanzas, hojas A.3.x) en data/perfil_vencimientos_usd.csv.
Uso: python generar_perfil_vencimientos.py deuda_publica_30-06-2026.xlsx [--salida data/perfil_vencimientos_usd.csv]
Toma la fila «TOTAL DEUDA EN MONEDA EXTRANJERA» (capital: A.3.2, A.3.4, A.3.7; interés: A.3.3, A.3.5, A.3.8). Montos en millones de USD."""
import argparse, re
import openpyxl
import pandas as pd

def fila(ws):
    """(fechas/etiquetas del encabezado, valores) de la fila de total en moneda extranjera."""
    enc = val = None
    for r in ws.iter_rows(max_col=60, values_only=True):
        v = [x for x in r if x is not None]
        if enc is None and len(v) >= 5 and all(isinstance(x, (int, float)) or hasattr(x, "year") for x in v[:3]):
            if hasattr(v[0], "year") or (isinstance(v[0], (int, float)) and 2000 < v[0] < 2100):
                enc = v
        for i, c in enumerate(r[:3]):
            if isinstance(c, str) and re.match(r"\s*TOTAL DEUDA EN MONEDA EXTRANJERA", c, re.I):
                val = [x for x in r[i + 1:] if x is not None]
                break
        if val is not None:
            break
    if enc is None or val is None:
        raise SystemExit(f"No encontré encabezado/fila de total en {ws.title}")
    return enc, val

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--salida", default="data/perfil_vencimientos_usd.csv")
    a = ap.parse_args()
    wb = openpyxl.load_workbook(a.xlsx, data_only=True)
    rows = []
    for tipo, mens, anual in (("capital", ("A.3.2", "A.3.4"), "A.3.7"), ("interes", ("A.3.3", "A.3.5"), "A.3.8")):
        for h in mens:
            enc, val = fila(wb[h])
            for e, x in zip(enc, val):
                if hasattr(e, "year"):
                    rows.append((f"{e:%Y-%m}", tipo, float(x)))
        enc, val = fila(wb[anual])
        for e, x in zip(enc, val):
            if isinstance(e, (int, float)) and int(e) >= 2028:
                rows.append((str(int(e)), tipo, float(x)))
            elif isinstance(e, str) and re.match(r"\d{4}-\d{4}$", e):
                rows.append((e, tipo, float(x)))
    d = pd.DataFrame(rows, columns=["periodo", "tipo", "usd_mn"]).pivot_table(index="periodo", columns="tipo", values="usd_mn", aggfunc="sum").reset_index()
    d["total"] = d["capital"] + d["interes"]
    d["fuente"] = "Secretaría de Finanzas, Deuda de la Administración Central, " + re.sub(r".*?(\d{2}-\d{2}-\d{4}).*", r"\1", a.xlsx)
    d.round(1).to_csv(a.salida, index=False)
    print(d.round(0).to_string(index=False))

if __name__ == "__main__":
    main()
