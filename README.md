# Backoffice Backend — Smart Inventory Platform

[![codecov](https://codecov.io/gh/Smart-Inventory-App/BackOffice_backend/branch/main/graph/badge.svg?token=QI2CYEL6L0)](https://codecov.io/gh/Smart-Inventory-App/BackOffice_backend)

**Backend** del módulo de **backoffice** (panel de administración) de Smart Inventory. Servicio paralelo al `app_backend`, separado en responsabilidades pero conectado a la **misma base de datos** (`tpp_db`) donde se almacena la información de los usuarios.

Cumple el rol descrito en la propuesta: control, monitoreo y habilitación de la plataforma por parte de usuarios administradores (estado general del sistema, gestión de usuarios, supervisión de los datos e imágenes cargados, y métricas/tendencias).

---

## Stack

- Python 3.11 + FastAPI
- PostgreSQL (SQLAlchemy) — **comparte `tpp_db`** con `auth` y `app_backend`
- JWT HS256 (python-jose) con **secreto propio de admin** (independiente del de los usuarios)
- bcrypt (passlib) para las contraseñas de administradores

Puerto: **8002**.

---

## Modelo de datos

| Tabla | Propietario | Uso en el backoffice |
|-------|-------------|----------------------|
| `admin_users` | **este servicio** | administradores del backoffice (se crea automáticamente) |
| `ocr_scan_audit` | **este servicio** | un renglón por ejecución del módulo de OCR; es lo único que queda de cada escaneo |
| `auth_users` | servicio `auth` | listar/habilitar/deshabilitar usuarios de la app |
| `products` | `app_backend` | visualizar/eliminar productos cargados por los usuarios |
| `user_profiles` | `app_backend` | datos de perfil (nombre, usuario, reglas de alerta, hora del chequeo, tipo de cuenta) |
| `alerts` | `app_backend` | se lee: las alertas del modelo predictivo (stock bajo por cobertura y ritmo lento) |
| `consumption_events` | `app_backend` | se lee y se escribe: cada entrada y salida de stock (tendencias, «Últimos cambios») |

> El backoffice **solo crea** sus propias tablas (`admin_users` y las de auditoría). Las demás ya las crean sus servicios dueños; acá solo se leen/gestionan filas.

---

## Endpoints (`/admin/*`, requieren token de admin salvo el login)

### Autenticación

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| POST | `/admin/auth/login` | Login de admin → `access_token` |
| GET | `/admin/auth/me` | Admin autenticado |
| GET | `/admin/auth/admins` | Listar administradores |
| POST | `/admin/auth/admins` | Agregar un administrador por su email. Con cuenta: la conserva. **Sin cuenta: `404` hasta que se manda `initial_password`**, con la que `auth` crea la cuenta; la persona la cambia en su primer ingreso (`must_change_password`). La respuesta trae `account_created` |
| POST | `/admin/auth/change-password` | Cambia la contraseña de quien tiene la sesión iniciada (lo hace `auth` con el mismo token) |

| POST | `/admin/auth/google` | **Ingreso y registro con Google**, igual que en la app: recibe el `id_token` que le dio Google a quien tocó «Continuar con Google» y se lo pasa a `auth` (`POST /auth/google`), que lo verifica, comprueba que lo emitió Google para nuestro cliente Web y crea la cuenta si es la primera vez. Devuelve el token de siempre. Un administrador dado de baja no entra; los errores salen con los mismos textos que en la app (token inválido, email sin verificar, Google inalcanzable, cuenta desactivada) |
| POST | `/admin/auth/forgot-password` | Pide a `auth` un código por mail para recuperar (o crear, en una cuenta de Google) la contraseña |
| POST | `/admin/auth/verify-code` | Verifica el código y devuelve el permiso para cambiar la contraseña |
| POST | `/admin/auth/reset-password` | Guarda la contraseña nueva |

`GET /admin/auth/me` devuelve `must_change_password`: el dashboard no deja usar nada hasta que se cambie la contraseña inicial.

`POST /admin/auth/check` además devuelve `google_only`: la cuenta se creó con Google y todavía no tiene contraseña (el ingreso ofrece crear una desde «Olvidé mi contraseña»).

### Dashboard y tendencias de la plataforma

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/admin/stats/` | Usuarios, productos y unidades, alertas críticas y advertencias de toda la plataforma (con las reglas de cada usuario), usuarios con alertas, consumo y reposición de los últimos 7 días, categorías |
| GET | `/admin/metrics?period=` | Tendencias de consumo de toda la plataforma (mismo formato que `/me/metrics`) |

### Usuarios

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/admin/users/` | Listar usuarios (filtros `search`, `status_filter`) |
| GET | `/admin/users/{id}` | Detalle de usuario: unidades, alertas críticas y advertencias, consumo y reposición de los últimos 30 días, sus alertas y sus productos con el nivel de alerta de cada uno |
| PATCH | `/admin/users/{id}/status` | Habilitar/deshabilitar (`is_active`) |

### Productos

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/admin/products/` | Listar productos (filtros `search`, `categoria`, `user_id`, `only_with_image`, `vencido`, `bajo_stock`, `alert_level`; orden `sort_by` / `sort_dir`; páginas `limit` / `offset`). Cada producto trae `alert_level` y `alert_types`, con las reglas de su dueño |
| GET | `/admin/products/export` | Exportar a CSV o Excel, con los mismos filtros y el mismo orden que el listado |
| GET | `/admin/products/filters/options` | Categorías y dueños disponibles para los filtros |
| GET | `/admin/products/groups` | Buscar por nombre los productos agrupados, con su stock total |
| GET | `/admin/products/group` | Todos los productos con un mismo nombre y categoría, y su stock total |
| GET | `/admin/products/{id}` | Detalle de producto |
| DELETE | `/admin/products/{id}` | Eliminar producto |

**Orden.** `sort_by` acepta `nombre`, `stock`, `vencimiento` y `categoria`. Por nombre el orden es alfabético de verdad (no distingue mayúsculas ni tildes, la «ñ» va después de la «n») y por vencimiento no entran los productos sin fecha. El id desempata siempre, así dos páginas seguidas no repiten ni se saltean productos. Lo mismo vale para los productos propios del administrador (`/me/products`) y para las exportaciones. La lógica está en `app/core/sorting.py`.

**Stock total por producto.** «El mismo producto» es el que tiene igual nombre y categoría, sin distinguir mayúsculas, tildes ni espacios sobrantes; la marca no entra en la clave.

- `GET /admin/products/groups?q=` devuelve hasta 10 grupos cuyo nombre contiene `q`, con `stock_total`, `lotes`, `usuarios` y `proximo_vencimiento`.
- `GET /admin/products/group?nombre=&categoria=` devuelve el detalle con todos los productos del grupo, ordenados por vencimiento (los que no vencen, al final), cada uno con su dueño. Responde `404` si no existe.
- Los dos aceptan `user_id`. **Con `user_id` se cuenta solo lo de ese usuario; sin él, lo de todos los usuarios de la plataforma**, que es lo que permite ver el stock total de un producto en toda la plataforma.

### Reglas y alertas (iguales a las de la app)

El backoffice calcula las alertas con **las mismas reglas que `app_backend`** (`app/core/alert_rules.py` y `app/core/alerts_view.py`), pero en su propio código: el panel sigue siendo un servicio separado.

- **Vencimiento**: se avisa cuando faltan **menos de** N días (o ya venció). **Stock mínimo**: se avisa cuando, al ritmo de consumo del modelo predictivo, el stock alcanza para **menos de** N días. En los dos casos manda la regla del producto y, si no tiene, la del perfil (7 días por defecto).
- **Nivel**: *crítica* si ya se llegó al límite, *advertencia* si se está cerca (hasta la mitad del límite más allá, con un mínimo de 2 días) y sin alerta si está lejos. El nivel se calcula **al leer**, con la fecha de hoy y la regla vigente, así un cambio de regla se ve enseguida.
- Las de **vencimiento** se calculan acá con los productos; las del **modelo predictivo** (`stock_low_coverage`, `slow_pace`) las guarda `app_backend` en la tabla `alerts` y acá se leen (su nivel también sale de la regla de hoy). Son las que llevan la marca «Predicción».
- Al cambiar una regla del perfil, o al cargar, editar o dar salida a un producto, el backoffice le pide a `app_backend` que vuelva a evaluar las alertas del usuario **sin mandar avisos** (`POST /internal/alerts/run/{user_id}?notify=false`). Es de mejor esfuerzo: si el servicio no responde, el cambio queda guardado igual.

### Hora argentina

Los eventos se guardan en UTC, pero **el día de cada movimiento, el «hoy» de los gráficos y de las alertas, y la hora del chequeo diario son los de Argentina (UTC-3)** (`app/core/argentina.py`). La hora del chequeo se guarda en UTC en la base (las 05:00 argentinas son las 8) y el perfil la muestra en hora argentina.

### Endpoints del usuario (`/me/*`, piden token de usuario)

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/me/stats` | Productos, unidades en stock, productos con una alerta crítica de vencimiento y de stock, vencidos, alertas críticas y advertencias, y lo que dibujan los gráficos del dashboard: unidades por categoría, productos por ubicación y cuándo vencen (vencidos, 0 a 7, 8 a 30, 31 a 90 y más de 90 días, sin vencimiento) |
| GET | `/me/alerts/` | Centro de alertas: filtros `tipo` (`stock` / `vencimiento`) y `level`; el resumen cuenta lo del tipo pedido, no lo del nivel |
| GET | `/me/alerts/summary` | Cuántas alertas hay por nivel |
| GET | `/me/changes?limit=&offset=` | «Últimos cambios»: entradas, salidas y modificaciones con el stock antes y después (se reconstruye hacia atrás desde el stock actual) |
| GET | `/me/metrics?period=` | Tendencias: consumo por día (días argentinos), totales y los **5** productos más consumidos (los lotes de un mismo nombre cuentan como uno); sin «menos usados» |
| GET | `/me/profile`, PUT | Perfil con los límites de la app: nombre, apellido y usuario hasta 20 caracteres, días de las alertas de 2 a 90, hora del chequeo de 0 a 23 (Argentina), tipo de cuenta |
| GET | `/me/profile/username-available?username=` | Si el nombre de usuario se puede usar; guardarlo repetido es un `409` (no distingue mayúsculas) |
| POST | `/me/products/image` | Sube la **imagen de un producto como archivo** (`multipart`, JPG/PNG/WebP de hasta `MAX_IMAGE_MB`) al servicio de almacenamiento con el token del usuario y devuelve `{ "url": ... }` |
| GET | `/me/products/match?q=` | Productos del usuario parecidos a un texto leído de una etiqueta: compara nombre, marca y descripción |
| GET | `/me/products/groups?q=`, `/me/products/group` | Stock total de «el mismo producto» entre los lotes del usuario |

El **código de barras** acepta 8, 12, 13 o 14 dígitos y **no exige el dígito verificador**, igual que la app; un UPC-A se guarda como EAN-13.

### Módulo de OCR

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/admin/ocr/status` | Salud del módulo: Redis, motor configurado, decodificador de códigos y versión de cada motor |
| POST | `/admin/ocr/scan` | Procesa una foto y devuelve **la lectura completa** (`multipart`: `file`, `doc_type`, `engine` opcional) |
| POST | `/admin/ocr/temp/purge` | Borra las imágenes de escaneo que quedaron sin descartar → `{ "purged": n }` |
| GET | `/admin/ocr/audit` | Historial de ejecuciones (filtros `user`, `doc_type`, `origen`) |
| GET | `/admin/ocr/stats` | Tiempos y confianza de los últimos `dias`, por tipo de documento |

El backoffice habla con **un solo servicio del módulo**: `ocr_service`, el coordinador. Los motores (Tesseract y PaddleOCR, este último replicado) viven detrás de él.

Dos diferencias deliberadas con el escaneo de la app, y las dos salen de que son usos opuestos:

- **Es sincrónico.** La app tiene a alguien esperando frente a un formulario de alta, así que encola y avisa por polling. Acá alguien subió una foto justamente para ver qué sale, y partirlo en dos llamadas no le ahorra nada.
- **Devuelve la lectura entera, no un borrador.** Confianza por campo, texto legible, qué motor ganó, con qué giro, cuántos intentos hicieron falta y qué advertencias hubo. Es lo que sirve para diagnosticar por qué una foto sale mal; el borrador de campos de alta es cosa de la app.

El escaneo de diagnóstico consume el mismo cupo por minuto que el de la app (60 códigos de barras, 10 etiquetas o vencimientos, 4 comprobantes): el coordinador lo cuenta contra el `sub` del token de servicio del backoffice, así que un `429` acá significa que se está diagnosticando muy rápido, no que el módulo esté caído.

`engine` permite forzar un motor (`tesseract`, `paddleocr`, `hybrid`) para compararlos sobre la misma foto. La imagen **no se guarda en ningún lado**: va directo al coordinador, que la procesa en memoria — a diferencia del escaneo de la app, que sí pasa por la carpeta temporal de `image_storage_service` porque su procesamiento es asincrónico.

La purga es una red de seguridad, no parte del flujo: el worker del OCR descarta la imagen temporal de cada job en un `finally`, y esto limpia lo que queda cuando el proceso muere en medio de uno (un OOM, un redeploy). Va contra el coordinador y no contra el storage, porque el storage es dependencia suya y no de este servicio.

`GET /admin/ocr/status` **no falla cuando el módulo está caído**: eso es justamente lo que el panel tiene que poder mostrar. Devuelve `disponible: false` y el diagnóstico que haya podido obtener.

#### Auditoría de Ejecuciones

Las fotos escaneadas **no se guardan en ningún lado**: el coordinador las sube a la carpeta temporal de `image_storage_service` y el worker las descarta al terminar. Eso es deliberado —una foto de la despensa o de una factura de alguien no tiene por qué quedar en ningún disco— pero deja al módulo sin historia, y sin historia no se puede saber si funciona ni quién lo usó.

La tabla `ocr_scan_audit` es el reemplazo: un renglón por ejecución con quién la pidió, de qué tipo era, qué motor ganó, cuánto tardó, con qué confianza salió y un **resumen recortado** de lo leído (160 caracteres). Alcanza para auditar el uso y para calcular tiempos y precisión, y no alcanza para reconstruir la imagen — que es exactamente el punto.

Se registran las tres entradas al módulo: el escaneo del panel, el código de barras del usuario (sincrónico, sin job) y los encolados, éstos al consultarse por primera vez ya terminados. El `job_id` va con índice único justamente por eso: la pantalla consulta cada medio segundo y sin ese corte quedaría un renglón por consulta.

**Auditar nunca hace fallar a quien escanea.** Si la escritura del renglón falla, queda en el log del servicio y el resultado de la foto se devuelve igual: perder una lectura de un minuto por un renglón de historial sería un mal negocio.

`GET /admin/ocr/stats` mide sobre esas ejecuciones reales, no sobre un banco de pruebas, y **separa por tipo de documento** porque mezclarlos no significa nada: un código de barras vuelve en décimas y o acierta o falla, y un comprobante tarda cerca de un minuto y casi siempre necesita revisión. El promedio de confianza es lo más parecido a una precisión que se puede dar sin conocer la respuesta correcta: es lo que el propio módulo dice de sus lecturas.

#### Carga por Foto del Usuario (`/me/products/ocr/*`)

El mismo módulo, con otra finalidad: acá no se diagnostica, se arma un borrador que el usuario corrige antes de confirmar. Estos endpoints piden token de usuario, no de admin.

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| POST | `/me/products/ocr/scan` | Manda una foto (`multipart`: `file`, `doc_type`). Un `barcode` vuelve resuelto (`200`); el resto, encolado (`202`) |
| GET | `/me/products/ocr/scan/{job_id}` | Estado del escaneo; al terminar, el borrador ya emparejado |
| POST | `/me/products/ocr/salida` | Descarga varios productos de un mismo comprobante, con motivo `Documento Comercial` |
| GET | `/me/products/by-barcode/{codigo}` | Qué producto tuyo tiene ese código, o `null` |

Dos diferencias con el escaneo de diagnóstico:

- **Viaja el token del usuario, no el de servicio.** El coordinador cuenta la cuota por minuto y guarda el dueño de cada job según el `sub` del token: firmar uno de servicio dejaría a todos los usuarios compartiendo cupo y pudiendo leer escaneos ajenos.
- **La lectura vuelve emparejada** con el inventario de quien escanea. Un nombre suelto no alcanza para decidir a qué producto sumarle o restarle stock, y crear un producto nuevo por cada abreviatura del comercio (`YOG SER FRUT` contra `Yogur Ser frutilla`) rompe el inventario en pedazos que después nadie junta.

El emparejamiento vive en `app/core/matching.py` y **no decide nada**: devuelve candidatos con su puntaje y el usuario confirma. Compara nombres normalizados (sin acentos, sin mayúsculas) y reconoce las abreviaturas de ticket, en las que cada palabra es prefijo de una del nombre completo. Debajo de `0.55` no se ofrece ningún candidato —uno malo es peor que ninguno, porque invita a confirmar sin leer— y a partir de `0.85` se preselecciona. Sin candidatos vuelve `texto_leido`, el nombre crudo, para que el usuario lo edite.

Un código de barras no pasa por ahí: no se parece a un producto, **es** ese producto, así que se busca por igualdad y con el código ya normalizado. Que no aparezca ninguno es un resultado normal, no un error: es lo que le dice al formulario que abra un alta nueva con el código puesto.

La salida por comprobante llega con los `product_id` que el usuario ya confirmó, no con nombres —volver a emparejar sería decidir de nuevo algo que la persona ya decidió mirando la foto— y queda asentada como consumo, que es lo que es para las métricas y para el ledger, con el motivo en la nota. Cada renglón se procesa por su cuenta y los que no se pueden aplicar vuelven explicados en `errores`, para que la pantalla pueda avisar cuántos quedaron afuera en vez de descargar de menos en silencio.

Las fotos de este flujo **no se guardan**: el coordinador las sube a la carpeta temporal de `image_storage_service` y el worker las descarta al terminar el job. Lo único que persiste es lo que el usuario confirma después, contra los endpoints de alta y de salida de siempre.

### Health

`GET /health`

---

## Configuración

Crear `.env` a partir de `.env.example`:

```env
DATABASE_URL=postgresql://user:password@localhost:5432/tpp_db
ADMIN_JWT_SECRET=admin-secret-change-in-production
ADMIN_JWT_ALGORITHM=HS256
ADMIN_JWT_EXPIRE_MINUTES=120
ALLOWED_ORIGINS_STR=http://localhost:3000,http://localhost:5173

# Módulo de OCR — el coordinador. Dentro de la red de Docker, el nombre del
# contenedor; corriendo a mano en la máquina, `http://localhost:8003`.
OCR_SERVICE_URL=http://tpp_ocr:8003
OCR_TIMEOUT_SECONDS=300

# Servicio de almacenamiento de imágenes: las fotos de producto se suben como archivo
IMAGE_STORAGE_URL=http://tpp_storage_service:8000
MAX_IMAGE_MB=10

DEBUG=True
HOST=0.0.0.0
PORT=8002
```

> El bloque `ADMIN_JWT_*` es histórico: hoy el backoffice **valida el mismo JWT que emite el servicio `auth`** (`JWT_SECRET` compartido con `app_backend`) y exige además que el email esté en la whitelist de `admin_users`. Ese mismo secreto es el que le permite firmar un token de servicio de vida corta para hablar con el coordinador de OCR, sin un segundo esquema de credenciales.
> El `ADMIN_JWT_SECRET` es **propio del backoffice** — no necesita coincidir con el `JWT_SECRET` de los usuarios de la app (son sesiones separadas).

---

## Setup local (Windows)

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Los administradores los gestiona el servicio `auth` (dueño de `admin_users`).
# Para dar de alta el primero, desde el repo de auth (imprime un código de
# invitación que el admin pide al definir su contraseña en su primer login):
#   python create_admin.py mail@dominio.com "Nombre Apellido"
# Después, un admin puede promover a cualquier usuario existente desde el panel.

# Levantar (requiere el servicio auth corriendo en AUTH_SERVICE_URL)
python main.py
```

Swagger: <http://localhost:8002/docs>

---

## Docker

```powershell
# requiere la red tpp-net (la crea el docker-compose de app_backend)
docker compose up -d
```
