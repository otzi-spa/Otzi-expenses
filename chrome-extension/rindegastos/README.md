# Otzi a Rindegastos

Extension Chrome unpacked para cargar el CSV exportado desde Expenses y rellenar el formulario de gastos multiples de Rindegastos.

## Uso local

1. Abrir `chrome://extensions`.
2. Activar `Developer mode`.
3. Click en `Load unpacked`.
4. Seleccionar la carpeta `chrome-extension/rindegastos`.
5. En Expenses, descargar `Exportar Rindegastos`.
6. En Rindegastos, abrir la pantalla de crear gastos multiples y crear la cantidad de gastos de la tanda.
7. Abrir la extension, cargar el CSV, seleccionar politica y tanda.
8. Usar `Copiar tanda` o `Rellenar Rindegastos`.

## Alcance Sprint 2

- Soporta CSV.
- Soporta tandas de 50 filas.
- Soporta `Vehiculo o Equipo` para Departamento Maquinaria y Departamento de Transporte.
- Soporta `Vehiculo o Equipo`, `Km carguío` y `Litros combustible` para Combustibles.
- Adjunta comprobantes cuando el CSV incluye `archivo_urls` firmadas desde Expenses.
- No presiona `Guardar cambios`.
- Intenta rellenar `ng-select` buscando por texto.

## Riesgos conocidos

- Algunos `ng-select` pueden requerir match exacto con el texto del catalogo de Rindegastos.
- La fecha es readonly; la extension remueve `readonly` y dispara eventos Angular. Si Rindegastos no acepta ese cambio, habra que implementar seleccion por datepicker en Sprint 3.
- La extensión detecta las columnas visibles por encabezado. Si Rindegastos cambia el nombre de un campo, hay que agregar su alias en `HEADER_FIELD_MAP`.
- Las URLs de comprobantes son temporales. Si vencen, hay que descargar nuevamente el CSV desde Expenses.
