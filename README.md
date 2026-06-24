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
DEBUG=True
HOST=0.0.0.0
PORT=8002
```

> El `ADMIN_JWT_SECRET` es **propio del backoffice** — no necesita coincidir con el `JWT_SECRET` de los usuarios de la app (son sesiones separadas).

---

## Setup local (Windows)

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Crear el primer administrador
# IMPORTANTE: se debe levantar la db del backoffice

# Usuario predeterminado: admin@smartinventory.com / contraseña: Admin1234
python create_admin.py
# Con credenciales propias:
python create_admin.py mail@dominio.com MiClave123 "Nombre Apellido"

# Levantar
python main.py
```

Swagger: http://localhost:8002/docs

---

## Docker

```powershell
# requiere la red tpp-net (la crea el docker-compose de app_backend)
docker compose up -d
```
