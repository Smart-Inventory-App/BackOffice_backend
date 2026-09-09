# Backoffice Backend — Smart Inventory

Backend del módulo de **backoffice** (panel de administración) de Smart Inventory. Servicio paralelo al `app_backend`, separado en responsabilidades pero conectado a la **misma base de datos** (`tpp_db`) donde se almacena la información de los usuarios.

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
| `auth_users` | servicio `auth` | listar/habilitar/deshabilitar usuarios de la app |
| `products` | `app_backend` | visualizar/eliminar productos cargados por los usuarios |
| `user_profiles` | `app_backend` | datos de perfil (nombre, usuario) |

> El backoffice **solo crea** la tabla `admin_users`. Las demás ya las crean sus servicios dueños; acá solo se leen/gestionan filas.

---

## Endpoints (`/admin/*`, requieren token de admin salvo el login)

### Autenticación
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| POST | `/admin/auth/login` | Login de admin → `access_token` |
| GET | `/admin/auth/me` | Admin autenticado |
| GET | `/admin/auth/admins` | Listar administradores |
| POST | `/admin/auth/admins` | Crear un nuevo administrador |

### Dashboard
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/admin/stats/` | Métricas globales (usuarios, productos, vencimientos, categorías, etc.) |

### Usuarios
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/admin/users/` | Listar usuarios (filtros `search`, `status_filter`) |
| GET | `/admin/users/{id}` | Detalle de usuario + sus productos |
| PATCH | `/admin/users/{id}/status` | Habilitar/deshabilitar (`is_active`) |

### Productos
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/admin/products/` | Listar productos (filtros `search`, `categoria`, `only_with_image`) |
| GET | `/admin/products/{id}` | Detalle de producto |
| DELETE | `/admin/products/{id}` | Eliminar producto |

### Módulo de OCR
| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/admin/ocr/status` | Salud del módulo: Redis, motor configurado, decodificador de códigos y versión de cada motor |
| POST | `/admin/ocr/scan` | Procesa una foto y devuelve **la lectura completa** (`multipart`: `file`, `doc_type`, `engine` opcional) |

El backoffice habla con **un solo servicio del módulo**: `ocr_service`, el coordinador. Los tres motores (Tesseract, EasyOCR, PaddleOCR) viven detrás de él.

Dos diferencias deliberadas con el escaneo de la app, y las dos salen de que son usos opuestos:

- **Es sincrónico.** La app tiene a alguien esperando frente a un formulario de alta, así que encola y avisa por polling. Acá alguien subió una foto justamente para ver qué sale, y partirlo en dos llamadas no le ahorra nada.
- **Devuelve la lectura entera, no un borrador.** Confianza por campo, texto legible, qué motor ganó, con qué giro, cuántos intentos hicieron falta y qué advertencias hubo. Es lo que sirve para diagnosticar por qué una foto sale mal; el borrador de campos de alta es cosa de la app.

`engine` permite forzar un motor (`tesseract`, `easyocr`, `paddleocr`, `hybrid`) para compararlos sobre la misma foto. La imagen **no se guarda en ningún lado**: va directo al coordinador, que la procesa en memoria — a diferencia del escaneo de la app, que sí pasa por la carpeta temporal de `image_storage_service` porque su procesamiento es asincrónico.

`GET /admin/ocr/status` **no falla cuando el módulo está caído**: eso es justamente lo que el panel tiene que poder mostrar. Devuelve `disponible: false` y el diagnóstico que haya podido obtener.

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

# Agregar un administrador a la whitelist (requiere la db levantada).
# NO se define contraseña acá: el admin la define en su primer login (vía auth).
python create_admin.py # admin por defecto: admin@smartinventory.com
python create_admin.py mail@dominio.com "Nombre Apellido"

# Levantar (requiere el servicio auth corriendo en AUTH_SERVICE_URL)
python main.py
```

Swagger: http://localhost:8002/docs

---

## Docker

```powershell
# requiere la red tpp-net (la crea el docker-compose de app_backend)
docker compose up -d
```
