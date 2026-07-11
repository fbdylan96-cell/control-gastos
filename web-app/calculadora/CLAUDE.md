# Reglas para trabajar en web-app/calculadora/

**Lee `README.md` de esta carpeta antes de tocar código.** Resumen de reglas duras:

1. **Un solo `src/`**: los motores viven únicamente en `calculadora-core/src/`. PROHIBIDO
   copiar `src/` o cualquiera de sus archivos dentro de `calculadora-estrategia/` o
   `calculadora-financiera/`. Las apps lo importan vía `sys.path` (ya está cableado).
2. **Motor ≠ UI**: cálculo puro en `calculadora-core/src/` (sin Streamlit); la interfaz en
   la app correspondiente.
3. **No rompas firmas**: antes de cambiar la firma de una función del core, busca todas sus
   llamadas en las DOS apps (`grep -rn "nombre" web-app/calculadora/`). Prefiere kwargs
   nuevos con valor default.
4. **No commitees**: `__pycache__/`, `*.pyc`, `venv/`, binarios pesados (ZIP/PPT), `.env`.
5. **Datos estáticos** (CSVs) van en `calculadora-core/assets/`; el caché de precios lo
   gestionan solos los clientes de `src/data/` en `calculadora-core/data_cache/`.
6. **Montos regulatorios CCSS** solo se cambian en `src/pension/` citando fuente y fecha.
7. Tras cambios en `calculadora-core/`, verifica que **ambas** apps siguen importando:
   `python -m compileall calculadora-core/src` y prueba los imports de las dos apps.
