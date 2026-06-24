"""Seed / create a backoffice administrator.

Usage:
    python create_admin.py <email> <password> [full_name]

If run with no arguments it falls back to a default dev admin:
    admin@smartinventory.com / Admin1234
"""
import sys

from app.core.database import Base, SessionLocal, engine
from app.core.security import hash_password
from app.models.admin import AdminUser


def main():
    Base.metadata.create_all(bind=engine)

    if len(sys.argv) >= 3:
        email = sys.argv[1]
        password = sys.argv[2]
        full_name = sys.argv[3] if len(sys.argv) >= 4 else None
    else:
        email = "admin@smartinventory.com"
        password = "Admin1234"
        full_name = "Administrador"
        print("No se pasaron argumentos: usando admin por defecto.")

    db = SessionLocal()
    try:
        existing = db.query(AdminUser).filter(AdminUser.email == email).first()
        if existing:
            print(f"Ya existe un administrador con el email {email}. No se creó nada.")
            return
        admin = AdminUser(
            email=email,
            hashed_password=hash_password(password),
            full_name=full_name,
        )
        db.add(admin)
        db.commit()
        print(f"Administrador creado: {email}")
        print(f"Contraseña: {password}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
