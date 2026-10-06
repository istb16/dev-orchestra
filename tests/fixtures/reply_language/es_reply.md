La revisión ya terminó. De los tres revisores, dos terminaron bien y uno falló por un tiempo de espera. Llegaron cuatro observaciones: dos aceptadas, una rechazada y otra fusionada como duplicado.

- F1 (high): la validación aceptaba una cadena en lugar de un booleano. El lado que lee la configuración trata todo lo que no es falso como activado, así que no hubo un daño real, pero ahora la comprobación es más estricta.
- F2 (medium): el script que envuelve el intérprete terminaba con un error cuando no encontraba uno adecuado. Ahora termina sin ruido, que es lo que se espera de un hook.

Todas las pruebas pasan de nuevo después de las correcciones. No queda nada por hacer en esta rama, y sugiero abrir la pull request ahora.
