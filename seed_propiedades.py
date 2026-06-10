from app import app, db, Propiedad
from datetime import date
from decimal import Decimal

PROPIEDADES = [
    {
        'direccion': '101 Bainbridge Circle, Gamer, NC 27529',
        'disponible': True,
        'costo_compra': Decimal('346904.07'),
        'fecha_compra': date(2026, 2, 24),
    },
    {
        'direccion': '114 N. Guthrie Avenue Durham NC, 27703',
        'disponible': True,
        'costo_compra': Decimal('408574.98'),
        'fecha_compra': date(2026, 2, 24),
    },
    {
        'direccion': '707 Wade Ave, Raleigh, NC 27605',
        'disponible': True,
        'costo_compra': Decimal('343460.83'),
        'fecha_compra': date(2026, 2, 24),
    },
    {
        'direccion': '1832 Sunny Drive, Bradenton, FL 34207',
        'disponible': True,
        'costo_compra': Decimal('118012.89'),
        'fecha_compra': date(2025, 10, 21),
    },
    {
        'direccion': '3208 Lassiter Street, Durham, NC 27707',
        'disponible': True,
        'costo_compra': Decimal('387042.79'),
        'fecha_compra': date(2026, 2, 24),
    },
    {
        'direccion': '3611 Cub Creek Road, Durham, NC 27704, Durham County',
        'disponible': True,
        'costo_compra': Decimal('296415.70'),
        'fecha_compra': date(2026, 2, 24),
    },
    {
        'direccion': '4621 Woodridge Dr., Raleigh, NC 27612',
        'disponible': True,
        'costo_compra': Decimal('574312.29'),
        'fecha_compra': date(2026, 2, 24),
    },
]


def seed():
    with app.app_context():
        insertadas = 0
        for datos in PROPIEDADES:
            if not Propiedad.query.filter_by(direccion=datos['direccion']).first():
                db.session.add(Propiedad(**datos))
                insertadas += 1
        db.session.commit()
        omitidas = len(PROPIEDADES) - insertadas
        if insertadas:
            print(f'  OK: {insertadas} propiedad/es cargada/s.')
        if omitidas:
            print(f'  Info: {omitidas} propiedad/es ya existian, no se duplicaron.')


if __name__ == '__main__':
    seed()
