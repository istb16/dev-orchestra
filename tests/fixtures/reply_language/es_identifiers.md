Resumen de la revisión: hay 3 observaciones y todas están corregidas.

- F1 (high): `scripts/orchestrator/config.py:1290` en `_validate_language` aceptaba una cadena para `rewrite`. LoadedConfig.language_settings() trata todo lo que no es False como activado, así que no hubo daño.
- F2 (medium): hooks/run terminaba con exit 127, como bin/dev-orchestra, cuando no encontraba Python 3.11 o superior. Ahora termina con 0.
- F3 (low): añadí al README.md, en la sección Compatibility, cómo se trata DEV_ORCHESTRA_DELEGATED.

```text
$ python tests/run_parallel.py
Ran 1234 tests in 42.0s
OK
```

Las pruebas pasan con `python tests/run_parallel.py`, y ruff y pyright no informan nada. Ver https://github.com/example/repo/pull/12 para el detalle.
