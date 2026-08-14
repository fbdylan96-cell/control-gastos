"""Pruebas del decodificador DSR de credit_products_update.

El DSR de Power BI comprime con tres mecanismos simultáneos y equivocarse
produce filas silenciosamente incorrectas, no errores. Estas pruebas fijan el
comportamiento con un payload que ejercita los tres a la vez.

Correr con:  python test_credit_products.py
"""
import sys

from credit_products_update import (N_API_COLUMNS, SourceError, decode_dsr,
                                    row_hash, to_storage_row)

fallos = []


def check(nombre, ok, detalle=""):
    print(("  OK   " if ok else "  FALLA") + f" {nombre}" + (f" — {detalle}" if detalle else ""))
    if not ok:
        fallos.append(nombre)


# ── 1. Los tres mecanismos a la vez: diccionario, repetición y nulo ──────────
# 4 columnas: [0] fecha, [1] string por diccionario, [2] string por diccionario,
# [3] número. Fila 2 repite las columnas 0 y 1; fila 3 tiene la 2 nula.
DSR = {
    "ValueDicts": {
        "D0": ["Coopeande No1", "Banco Nacional"],
        "D1": ["Colón", "Dólar estadounidense"],
    },
    "PH": [{"DM0": [
        {"S": [{"N": "G0", "T": 7}, {"N": "G1", "T": 1, "DN": "D0"},
               {"N": "G2", "T": 1, "DN": "D1"}, {"N": "G3", "T": 3}],
         "C": [1785888000000, 0, 0, 8.5]},
        # R = 0b0011 → repite columnas 0 y 1; C solo trae las columnas 2 y 3
        {"R": 3, "C": [1, 12.0]},
        # Ø = 0b0100 → columna 2 nula; R = 0b0011 repite 0 y 1
        {"R": 3, "Ø": 4, "C": [9.75]},
    ]}],
    "RT": [["datetime'2026-08-05T00:00:00'"]],
}

print("1. Decodificación con diccionario + repetición + nulo")
filas, rt, esquema = decode_dsr(DSR, 4)
check("devuelve 3 filas", len(filas) == 3, f"devolvió {len(filas)}")
check("fila 1 resuelve los diccionarios",
      filas[0] == [1785888000000, "Coopeande No1", "Colón", 8.5], str(filas[0]))
check("fila 2 repite col 0 y 1, resuelve col 2 con SU diccionario",
      filas[1] == [1785888000000, "Coopeande No1", "Dólar estadounidense", 12.0],
      str(filas[1]))
check("fila 3 aplica el nulo y sigue repitiendo",
      filas[2] == [1785888000000, "Coopeande No1", None, 9.75], str(filas[2]))
check("devuelve el restart token", rt == DSR["RT"])
check("devuelve el esquema para la página siguiente", esquema is not None)

# ── 2. El esquema persiste entre páginas ────────────────────────────────────
print("\n2. Segunda página sin 'S': el esquema viene de la anterior")
PAGINA2 = {"ValueDicts": DSR["ValueDicts"],
           "PH": [{"DM0": [{"C": [1785888000000, 1, 0, 15.0]}]}]}
filas2, _, _ = decode_dsr(PAGINA2, 4, schema=esquema)
check("resuelve el diccionario con el esquema heredado",
      filas2[0] == [1785888000000, "Banco Nacional", "Colón", 15.0], str(filas2[0]))

# ── 3. Bitmask de más de 32 bits (36 columnas) ──────────────────────────────
print("\n3. Bitmask sobre 32 bits (la trampa de las 36 columnas)")
BIG = {"ValueDicts": {},
       "PH": [{"DM0": [
           {"S": [{"N": f"G{i}", "T": 4} for i in range(36)],
            "C": list(range(36))},
           # repite las 36 excepto la última (bit 35 apagado)
           {"R": (1 << 35) - 1, "C": [999]},
       ]}]}
filas3, _, _ = decode_dsr(BIG, 36)
check("la fila 2 repite las columnas 0..34",
      filas3[1][:35] == list(range(35)), str(filas3[1][:5]) + "…")
check("y toma el valor nuevo en la columna 35 (bit 35)",
      filas3[1][35] == 999, str(filas3[1][35]))

# ── 4. Un DSR inconsistente falla fuerte, no en silencio ────────────────────
print("\n4. DSR inconsistente")
MALO = {"ValueDicts": {}, "PH": [{"DM0": [
    {"S": [{"N": "G0", "T": 4}, {"N": "G1", "T": 4}], "C": [1]},  # falta un valor
]}]}
try:
    decode_dsr(MALO, 2)
    check("levanta SourceError", False, "no levantó nada")
except SourceError:
    check("levanta SourceError", True)
except Exception as e:
    check("levanta SourceError", False, f"levantó {type(e).__name__}")

# ── 5. Fila de referencia verificada contra la API real ─────────────────────
print("\n5. Fila de referencia (§7 de extractorbccrmeic.md)")
ref = [None] * N_API_COLUMNS
ref[1] = "3004045027"          # IdOferente
ref[2] = "Coopeande No1"       # NombreOferente
ref[3] = "Cooperativas"        # GrupoOferente
ref[4] = 1785888000000         # Periodo
ref[6] = "Actividades inmobiliarias y construcción"
ref[13] = "CREDITO VIVIENDA GENERAL COL"
ref[15] = "Colón"
ref[16] = 360
ref[17] = 10
ref[19] = "Tasa fija-variable"
ref[20] = 8.5
ref[21] = 8.5
ref[25] = "Formalización"
ref[26] = 1
ref[28] = "Porcentaje"
guardada = to_storage_row(ref)
check("recorta a 30 columnas", len(guardada) == 30, f"quedaron {len(guardada)}")
check("convierte el epoch ms a date 2026-08-05",
      str(guardada[4]) == "2026-08-05", str(guardada[4]))
check("conserva IdOferente como texto",
      guardada[1] == "3004045027" and isinstance(guardada[1], str))

# ── 6. row_hash: estable, y sensible solo a lo que se guarda ────────────────
print("\n6. row_hash")
h1 = row_hash(guardada)
check("es determinista", h1 == row_hash(list(guardada)))
check("mide 32 bytes (sha256)", len(h1) == 32, f"midió {len(h1)}")
otra = list(ref)
otra[30] = "variante en minúsculas"   # NombreProducto2, columna NO guardada
check("ignora las columnas de formato que no se guardan",
      row_hash(to_storage_row(otra)) == h1)
distinta = list(guardada)
distinta[29] = "otra observación"     # fee_notes, columna guardada
check("distingue un cambio en una columna guardada",
      row_hash(distinta) != h1)
check("distingue None de cadena vacía",
      row_hash([None, "a"]) != row_hash(["", "a"]))

print("\n" + ("TODAS LAS PRUEBAS PASARON" if not fallos
              else f"{len(fallos)} PRUEBA(S) FALLARON: {', '.join(fallos)}"))
sys.exit(1 if fallos else 0)
