"""Alta de un administrador del backoffice (whitelist).

Solo agrega el email a la whitelist de administradores. La contraseña la define
el propio admin en su primer inicio de sesión (a través del servicio auth).

Uso:
    python create_admin.py <email> "<Nombre Apellido>"

Sin argumentos crea un admin por defecto:
    admin@smartinventory.com
"""
import sys

from app.core.database import Base, SessionLocal, engine
from app.models.admin import AdminUser


def main():
    Base.metadata.create_all(bind=engine)

    if len(sys.argv) >= 2:
        email = sys.argv[1]
        full_name = sys.argv[2] if len(sys.argv) >= 3 else None
    else:
        email = "admin@smartinventory.com"
        full_name = "Administrador"
        print("No se pasaron argumentos: usando admin por defecto.")

    db = SessionLocal()
    try:
        existing = db.query(AdminUser).filter(AdminUser.email == email).first()
        if existing:
            print(f"Ya existe un administrador con el email {email}. No se creó nada.")
            return
        admin = AdminUser(email=email, full_name=full_name, password_set=False)
        db.add(admin)
        db.commit()
        print(f"Administrador agregado a la whitelist: {email}")
        print("Definirá su contraseña en el primer inicio de sesión del backoffice.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
