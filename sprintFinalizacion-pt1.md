# Sprint Finalizacion PT1 - Integracion Expenses a Rindegastos

## Contexto

El objetivo es mover gastos desde esta app hacia Rindegastos usando una extension de Google Chrome.

El flujo propuesto es:

1. Exportar gastos desde Expenses en un formato Excel/CSV controlado.
2. Usar una extension de Chrome para leer ese archivo.
3. Seleccionar politicas y tandas de gastos.
4. Rellenar el formulario de "Crear gastos multiples" en Rindegastos.
5. Dejar fuera del MVP la carga automatica de archivos/comprobantes.

## Decisiones base

- En la UI de Expenses, "Categoria" pasara a llamarse "Politica".
- Para minimizar riesgo inicial, se puede mantener internamente el campo/modelo `category` y cambiar solo la experiencia de usuario/exportacion. Una migracion completa a `policy` puede quedar para una etapa posterior.
- La politica debe exportarse como primera columna del archivo.
- El exportador debe incluir resumen por politica para saber cuantos gastos crear en Rindegastos.
- La extension debe soportar tandas, considerando que Rindegastos puede requerir crear gastos en grupos y que mas de 50 gastos deben manejarse en mas de una tanda.
- Los archivos/comprobantes quedan fuera del MVP.

## Politicas Rindegastos

Estas politicas deben existir en el mantenedor actual de categorias/politicas:

- Departamento Maquinaria
- Oficina Central
- Combustibles
- Autopista de Antofagasta 2025
- Vialidad Choapa COMA
- Vialidad Puerto Aysen
- Vialidad Coyhaique
- Vialidad Cochrane Lechada
- Embalse los Aromos III
- Curimon III
- Autopista de Antofagasta 2026

## Sprint 1: Expenses + Export base

Objetivo: dejar la app lista para producir un archivo confiable para Rindegastos.

Tareas:

1. Renombrar "Categoria" a "Politica" en la UI.
   - Cambiar labels, textos, tabla, filtros, modal y mantenedor.
   - Mantener internamente `category` si evita una migracion innecesaria.

2. Actualizar el mantenedor de categorias/politicas.
   - Que funcione como mantenedor de politicas.
   - Cargar o asegurar las politicas oficiales de Rindegastos.

3. Ordenar el modal de gastos por obligatoriedad.
   - Primero campos necesarios para Rindegastos.
   - Luego campos utiles opcionales.
   - Luego notas y comprobantes.
   - Revisar campos marcados con `data-required-param="true"` y remover obligatoriedad donde no sea necesaria para Rindegastos.

4. Crear exportador Rindegastos.
   - Default: exportar gastos parametrizados.
   - Agregar opcion para exportar todos los estados.
   - Agregar filtros por periodo desde/hasta.
   - Incluir primera columna `politica`.
   - Incluir resumen por politica.

Columnas sugeridas:

```text
politica
proveedor
total
moneda
impuesto
valor_impuesto
otros_impuestos
fecha
centro_costo_faena
nombre_quien_rinde
numero_documento
rut_proveedor
tipo_documento
vehiculo_equipo
categoria_rindegastos
nota
expense_id
archivo_url
```

Nota: conviene separar `politica` de `categoria_rindegastos`, porque pueden ser catalogos distintos.

## Sprint 2: Extension Chrome MVP

Objetivo: copiar datos por politica y por tanda, sin archivos.

Tareas:

1. Crear extension local unpacked.
   - `manifest.json` Manifest V3.
   - `popup.html`.
   - `popup.js`.
   - `content-script.js`.

2. Leer archivo exportado.
   - Soportar CSV primero si simplifica el MVP.
   - Soportar XLSX despues si es necesario.
   - Leer resumen por politica.

3. UI de extension.
   - Mostrar politicas con conteo de gastos.
   - Permitir seleccionar una o mas politicas.
   - Permitir seleccionar tanda: 1-50, 51-100, etc.
   - Mostrar preview de filas a copiar/pegar.
   - Mostrar warnings de campos obligatorios vacios.

4. Flujo de portapapeles/formulario.
   - Generar tabla copiable para la tanda seleccionada.
   - Preparar automatizacion para rellenar el formulario de Rindegastos.
   - Crear o validar cantidad de filas necesarias.
   - Completar inputs normales.
   - Completar `ng-select`.
   - No adjuntar archivos.
   - No presionar "Guardar cambios"; dejar revision manual.

## Sprint 3: Robustez Rindegastos

Objetivo: hacer la extension tolerante a datos reales y a los componentes Angular de Rindegastos.

Tareas:

1. Resolver `ng-select` de forma robusta.
   - Match exacto.
   - Fallback por texto parcial.
   - Reportar si no encuentra opcion.
   - Validar visualmente que quedo seleccionado.

2. Manejar fechas readonly.
   - Intentar setear valor con eventos Angular.
   - Si no funciona, abrir datepicker y seleccionar fecha.

3. Validaciones de negocio.
   - Politica sin gastos.
   - Campos obligatorios faltantes.
   - Moneda no soportada.
   - Politica no reconocida.
   - Mas de 50 gastos dividido automaticamente en tandas.

4. Reporte final de ejecucion.
   - Filas pegadas.
   - Filas omitidas.
   - Errores por campo.

## Sprint 4: Archivos / Comprobantes

Objetivo: evaluar y eventualmente automatizar adjuntos.

Este sprint queda fuera del MVP.

Opciones:

1. Mostrar links de comprobantes por gasto.
2. Permitir descarga manual desde Expenses.
3. Intentar carga automatica con `DataTransfer` si Rindegastos lo permite.
4. Validar que Angular/dropzone registre correctamente los archivos.

## Primer abordaje recomendado

1. Completar Sprint 1 en Expenses.
2. Crear Sprint 2 con CSV y extension local unpacked.
3. Probar con una politica y pocos gastos.
4. Agregar tandas de 50.
5. Endurecer `ng-select` y fechas en Sprint 3.
6. Evaluar archivos solo cuando el flujo de datos sea estable.
