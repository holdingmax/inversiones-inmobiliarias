import os
from datetime import datetime, date
from decimal import Decimal
from functools import wraps

from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, Response)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

database_url = os.environ.get('DATABASE_URL', 'sqlite:///inversiones.db')
# Render usa "postgres://" pero SQLAlchemy requiere "postgresql://"
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB máx. para imágenes

db = SQLAlchemy(app)


# ─── Modelos ──────────────────────────────────────────────────────────────────

class Usuario(db.Model):
    __tablename__ = 'usuarios'
    cod = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nombre_usuario = db.Column(db.String(30), unique=True, nullable=False)
    password = db.Column(db.String(256), nullable=False)


class Propiedad(db.Model):
    __tablename__ = 'propiedades'
    cod = db.Column(db.Integer, primary_key=True, autoincrement=True)
    direccion = db.Column(db.String(50), nullable=False)
    disponible = db.Column(db.Boolean, default=True)
    costo_compra = db.Column(db.Numeric(15, 2))
    fecha_compra = db.Column(db.Date)
    imagen = db.Column(db.LargeBinary)
    imagen_tipo = db.Column(db.String(50))
    transacciones = db.relationship('Transaccion', backref='propiedad',
                                    lazy=True, cascade='all, delete-orphan')


class Concepto(db.Model):
    __tablename__ = 'conceptos'
    cod = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nombre = db.Column(db.String(30), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'ingreso' | 'egreso'


class Moneda(db.Model):
    __tablename__ = 'monedas'
    cod = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nombre = db.Column(db.String(30), nullable=False)
    signo = db.Column(db.String(3), nullable=False)


class Transaccion(db.Model):
    __tablename__ = 'transacciones'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    fecha = db.Column(db.Date, nullable=False)
    propiedad_cod = db.Column(db.Integer, db.ForeignKey('propiedades.cod'), nullable=False)
    concepto_cod = db.Column(db.Integer, db.ForeignKey('conceptos.cod'), nullable=False)
    importe = db.Column(db.Numeric(15, 2), nullable=False)
    moneda_cod = db.Column(db.Integer, db.ForeignKey('monedas.cod'), nullable=False)
    observaciones = db.Column(db.String(50))
    concepto_rel = db.relationship('Concepto')
    moneda_rel = db.relationship('Moneda')


# ─── Decorador de autenticación ───────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'usuario_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ─── Rutas de autenticación ───────────────────────────────────────────────────

@app.route('/', methods=['GET', 'POST'])
def login():
    if 'usuario_id' in session:
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        nombre = request.form.get('nombre_usuario', '').strip()
        pwd = request.form.get('password', '')
        usuario = Usuario.query.filter_by(nombre_usuario=nombre).first()
        if usuario and check_password_hash(usuario.password, pwd):
            session['usuario_id'] = usuario.cod
            session['usuario_nombre'] = usuario.nombre_usuario
            return redirect(url_for('dashboard'))
        error = 'Usuario o contraseña incorrectos.'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')


# ─── Propiedad ────────────────────────────────────────────────────────────────

@app.route('/propiedad')
@login_required
def propiedad():
    propiedades = Propiedad.query.order_by(Propiedad.direccion).all()
    prop_sel = None
    transacciones = []
    total_ingresos = Decimal('0')
    total_egresos = Decimal('0')

    cod = request.args.get('cod', type=int)
    if cod:
        prop_sel = db.session.get(Propiedad, cod)
        if prop_sel:
            transacciones = (Transaccion.query
                             .filter_by(propiedad_cod=cod)
                             .order_by(Transaccion.fecha)
                             .all())
            for t in transacciones:
                if t.concepto_rel.tipo == 'ingreso':
                    total_ingresos += t.importe
                else:
                    total_egresos += t.importe

    return render_template('propiedad.html',
                           propiedades=propiedades,
                           prop_sel=prop_sel,
                           transacciones=transacciones,
                           total_ingresos=total_ingresos,
                           total_egresos=total_egresos,
                           total_neto=total_ingresos - total_egresos,
                           cod_sel=cod)


@app.route('/propiedad/imagen/<int:cod>')
@login_required
def propiedad_imagen(cod):
    prop = db.session.get(Propiedad, cod)
    if not prop or not prop.imagen:
        return '', 404
    return Response(prop.imagen, mimetype=prop.imagen_tipo or 'image/jpeg')


# ─── Carga de información ─────────────────────────────────────────────────────

@app.route('/carga', methods=['GET', 'POST'])
@login_required
def carga():
    propiedades = Propiedad.query.order_by(Propiedad.direccion).all()
    conceptos = Concepto.query.order_by(Concepto.nombre).all()
    monedas = Moneda.query.order_by(Moneda.nombre).all()

    if request.method == 'POST':
        try:
            t = Transaccion(
                fecha=datetime.strptime(request.form['fecha'], '%Y-%m-%d').date(),
                propiedad_cod=int(request.form['propiedad_cod']),
                concepto_cod=int(request.form['concepto_cod']),
                importe=Decimal(request.form['importe']),
                moneda_cod=int(request.form['moneda_cod']),
                observaciones=request.form.get('observaciones', '').strip()[:50]
            )
            db.session.add(t)
            db.session.commit()
            flash('Transacción registrada correctamente.', 'success')
            return redirect(url_for('carga'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error al guardar: {e}', 'danger')

    return render_template('carga.html',
                           propiedades=propiedades,
                           conceptos=conceptos,
                           monedas=monedas,
                           hoy=date.today().isoformat())


# ─── Reportes ─────────────────────────────────────────────────────────────────

@app.route('/reportes')
@login_required
def reportes():
    propiedades = Propiedad.query.order_by(Propiedad.direccion).all()
    resumen = []
    for p in propiedades:
        ingresos = sum(
            (t.importe for t in p.transacciones if t.concepto_rel.tipo == 'ingreso'),
            Decimal('0')
        )
        egresos = sum(
            (t.importe for t in p.transacciones if t.concepto_rel.tipo == 'egreso'),
            Decimal('0')
        )
        resumen.append({
            'propiedad': p,
            'ingresos': ingresos,
            'egresos': egresos,
            'neto': ingresos - egresos
        })
    return render_template('reportes.html', resumen=resumen)


# ─── Configuración ────────────────────────────────────────────────────────────

@app.route('/configuracion')
@login_required
def configuracion():
    return render_template('configuracion.html',
                           propiedades=Propiedad.query.order_by(Propiedad.direccion).all(),
                           conceptos=Concepto.query.order_by(Concepto.nombre).all(),
                           monedas=Moneda.query.order_by(Moneda.nombre).all(),
                           usuarios=Usuario.query.order_by(Usuario.nombre_usuario).all())


# Propiedades CRUD
@app.route('/config/propiedad/add', methods=['POST'])
@login_required
def config_propiedad_add():
    try:
        imagen_data = None
        imagen_tipo = None
        if 'imagen' in request.files:
            f = request.files['imagen']
            if f and f.filename:
                imagen_data = f.read()
                imagen_tipo = f.content_type

        fecha_str = request.form.get('fecha_compra', '').strip()
        costo_str = request.form.get('costo_compra', '').strip()

        p = Propiedad(
            direccion=request.form['direccion'].strip()[:50],
            disponible=request.form.get('disponible') == 'on',
            costo_compra=Decimal(costo_str) if costo_str else None,
            fecha_compra=datetime.strptime(fecha_str, '%Y-%m-%d').date() if fecha_str else None,
            imagen=imagen_data,
            imagen_tipo=imagen_tipo
        )
        db.session.add(p)
        db.session.commit()
        flash('Propiedad agregada correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('configuracion') + '#propiedades')


@app.route('/config/propiedad/delete/<int:cod>', methods=['POST'])
@login_required
def config_propiedad_delete(cod):
    p = db.session.get(Propiedad, cod)
    if not p:
        flash('Propiedad no encontrada.', 'warning')
        return redirect(url_for('configuracion'))
    try:
        db.session.delete(p)
        db.session.commit()
        flash('Propiedad eliminada.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('configuracion') + '#propiedades')


# Conceptos CRUD
@app.route('/config/concepto/add', methods=['POST'])
@login_required
def config_concepto_add():
    try:
        c = Concepto(
            nombre=request.form['nombre'].strip()[:30],
            tipo=request.form['tipo']
        )
        db.session.add(c)
        db.session.commit()
        flash('Concepto agregado.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('configuracion') + '#conceptos')


@app.route('/config/concepto/delete/<int:cod>', methods=['POST'])
@login_required
def config_concepto_delete(cod):
    c = db.session.get(Concepto, cod)
    if not c:
        flash('Concepto no encontrado.', 'warning')
        return redirect(url_for('configuracion'))
    try:
        db.session.delete(c)
        db.session.commit()
        flash('Concepto eliminado.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('configuracion') + '#conceptos')


# Monedas CRUD
@app.route('/config/moneda/add', methods=['POST'])
@login_required
def config_moneda_add():
    try:
        m = Moneda(
            nombre=request.form['nombre'].strip()[:30],
            signo=request.form['signo'].strip()[:3]
        )
        db.session.add(m)
        db.session.commit()
        flash('Moneda agregada.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('configuracion') + '#monedas')


@app.route('/config/moneda/delete/<int:cod>', methods=['POST'])
@login_required
def config_moneda_delete(cod):
    m = db.session.get(Moneda, cod)
    if not m:
        flash('Moneda no encontrada.', 'warning')
        return redirect(url_for('configuracion'))
    try:
        db.session.delete(m)
        db.session.commit()
        flash('Moneda eliminada.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('configuracion') + '#monedas')


# Usuarios CRUD
@app.route('/config/usuario/add', methods=['POST'])
@login_required
def config_usuario_add():
    try:
        nombre = request.form['nombre_usuario'].strip()[:30]
        pwd = request.form['password']
        if Usuario.query.filter_by(nombre_usuario=nombre).first():
            flash('El usuario ya existe.', 'warning')
            return redirect(url_for('configuracion') + '#usuarios')
        u = Usuario(
            nombre_usuario=nombre,
            password=generate_password_hash(pwd)
        )
        db.session.add(u)
        db.session.commit()
        flash('Usuario agregado.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('configuracion') + '#usuarios')


@app.route('/config/usuario/delete/<int:cod>', methods=['POST'])
@login_required
def config_usuario_delete(cod):
    if cod == session.get('usuario_id'):
        flash('No podés eliminar tu propio usuario.', 'warning')
        return redirect(url_for('configuracion') + '#usuarios')
    u = db.session.get(Usuario, cod)
    if not u:
        flash('Usuario no encontrado.', 'warning')
        return redirect(url_for('configuracion'))
    try:
        db.session.delete(u)
        db.session.commit()
        flash('Usuario eliminado.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('configuracion') + '#usuarios')


# ─── Inicialización ───────────────────────────────────────────────────────────

def init_db():
    db.create_all()
    # Usuario admin por defecto si no hay ninguno
    if not Usuario.query.first():
        admin = Usuario(
            nombre_usuario='admin',
            password=generate_password_hash('admin123')
        )
        db.session.add(admin)
        # Datos iniciales de monedas
        db.session.add_all([
            Moneda(nombre='Peso Argentino', signo='$'),
            Moneda(nombre='Dólar Estadounidense', signo='USD'),
        ])
        db.session.commit()


with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(debug=True)
