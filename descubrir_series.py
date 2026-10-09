"""
descubrir_series.py - Busca IDs de series en la API de Series de Tiempo de datos.gob.ar y verifica que respondan.
Solo usa la libreria estandar. Pensado para correrse a mano cuando hay que sumar series a fetch_macro.py.

Uso:
    python descubrir_series.py buscar "exportaciones oro" "exportaciones litio" "petroleo crudo"
    python descubrir_series.py buscar "exportaciones" --mensual --desde 2026-01     # solo mensuales con dato reciente
    python descubrir_series.py explorar "Bienes Cobros" --prefijo 184.1 --mensual     # recorre TODAS las paginas y filtra por prefijo de id
    python descubrir_series.py explorar "exportaciones" --prefijo 75.3 --texto litio,oro # idem, filtrando ademas por texto en la descripcion
    python descubrir_series.py verificar 74.3_IET_0_M_16 75.3_ITCE_0_M_30           # ultimos valores de cada id
    python descubrir_series.py verificar --csv ids.txt                               # ids desde un archivo (uno por linea)

Salida: una linea por serie con id, descripcion, frecuencia (R/P1M = mensual, R/P3M = trimestral, R/P1Y = anual),
rango de fechas y unidad. Copiar el id a SERIES en fetch_macro.py (src="datos", agg="none").
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://apis.datos.gob.ar/series/api"
UA = "rem-macro-fetch/1.0 (+https://github.com/mau1878/REM)"


def get(path, params, retries=3):
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 404):
                break
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(2 ** i)
    raise RuntimeError(f"{url} -> {last}")


def fila(item):
    f, d = item.get("field", {}), item.get("dataset", {})
    return {"id": f.get("id", ""), "desc": (f.get("description") or f.get("title") or "")[:110],
            "freq": f.get("frequency", ""), "desde": str(f.get("time_index_start", ""))[:10],
            "hasta": str(f.get("time_index_end", ""))[:10], "unidad": f.get("units", ""), "dataset": d.get("title", "")[:60]}


def buscar(args):
    vistos = set()
    for q in args.consultas:
        print(f"\n=== {q}")
        js = get("search/", {"q": q, "limit": args.limite})
        n = 0
        for it in js.get("data", []):
            r = fila(it)
            if r["id"] in vistos:
                continue
            if args.mensual and r["freq"] != "R/P1M":
                continue
            if args.desde and r["hasta"] < args.desde:
                continue
            vistos.add(r["id"])
            n += 1
            print(f"{r['id']:<34} {r['freq']:<7} {r['desde']}..{r['hasta']}  {r['desc']}  | {r['dataset']}")
        if not n:
            print("  (sin resultados con esos filtros)")


def explorar(args):
    """El buscador de la API rankea por texto y no filtra por dataset: para listar un dataset completo hay que paginar (offset)
    y quedarse con los ids que empiezan con el prefijo (ej. 184.1 = cobros/pagos de bienes por sector del Balance Cambiario)."""
    textos = [t.strip().lower() for t in (args.texto or "").split(",") if t.strip()]
    vistos, n, pag = set(), 0, 0
    while pag < args.paginas:
        js = get("search/", {"q": args.consulta, "limit": 100, "offset": pag * 100})
        data = js.get("data", [])
        if not data:
            break
        for it in data:
            r = fila(it)
            if r["id"] in vistos:
                continue
            if args.prefijo and not r["id"].startswith(args.prefijo):
                continue
            if args.mensual and r["freq"] != "R/P1M":
                continue
            if args.desde and r["hasta"] < args.desde:
                continue
            if textos and not any(t in r["desc"].lower() for t in textos):
                continue
            vistos.add(r["id"])
            n += 1
            print(f"{r['id']:<34} {r['freq']:<7} {r['desde']}..{r['hasta']}  {r['desc']}  | {r['dataset']}")
        pag += 1
    print(f"\n{n} series ({pag} paginas recorridas, {args.paginas} maximo; subir --paginas si el dataset es grande)")


def verificar(args):
    ids = list(args.ids)
    if args.csv:
        ids += [x.strip() for x in open(args.csv, encoding="utf-8") if x.strip() and not x.startswith("#")]
    mal = 0
    for i in ids:
        try:
            js = get("series/", {"ids": i, "limit": 3, "sort": "desc", "format": "json", "metadata": "full"})
            data = js.get("data", [])
            meta = next((m for m in js.get("meta", []) if isinstance(m, dict) and "field" in m), {})
            if not data:
                raise RuntimeError("sin datos")
            ult = ", ".join(f"{d[0]}: {d[1]}" for d in data)
            print(f"OK   {i:<34} {meta.get('field', {}).get('description', '')[:70]} | {ult}")
        except Exception as e:  # noqa: BLE001
            mal += 1
            print(f"FALLA {i}: {e}")
    return 1 if mal else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("buscar")
    b.add_argument("consultas", nargs="+")
    b.add_argument("--limite", type=int, default=40)
    b.add_argument("--mensual", action="store_true", help="solo series mensuales")
    b.add_argument("--desde", help="solo series cuyo ultimo dato sea >= YYYY-MM (ej. 2026-01)")
    e = sub.add_parser("explorar")
    e.add_argument("consulta")
    e.add_argument("--prefijo", help="solo ids que empiezan con esto (ej. 184.1, 75.3)")
    e.add_argument("--texto", help="palabras separadas por coma; basta que una aparezca en la descripcion")
    e.add_argument("--paginas", type=int, default=10, help="paginas de 100 resultados a recorrer (default 10)")
    e.add_argument("--mensual", action="store_true")
    e.add_argument("--desde", help="ultimo dato >= YYYY-MM")
    v = sub.add_parser("verificar")
    v.add_argument("ids", nargs="*")
    v.add_argument("--csv", help="archivo con un id por linea")
    a = ap.parse_args()
    if a.cmd == "buscar":
        buscar(a)
        return 0
    if a.cmd == "explorar":
        explorar(a)
        return 0
    return verificar(a)


if __name__ == "__main__":
    sys.exit(main())
