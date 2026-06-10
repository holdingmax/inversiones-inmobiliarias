import os
import uuid
import json
import csv
import io
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, Response)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import pandas as pd
    PANDAS_OK = True
except ImportError:
    PANDAS_OK = False

# Almacenamiento temporal de imports pendientes (en memoria, válido para instancia única)
_pending_imports: dict = {}

COLUMNAS_REQUERIDAS = {'fecha', 'propiedad_cod', 'concepto_cod', 'importe', 'moneda_cod'}
FORMATOS_FECHA = ('%d/%m/%Y',)  # Único formato aceptado: DD/MM/AAAA

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
    moneda_cod = db.Column(db.Integer, db.ForeignKey('monedas.cod'))
    fecha_compra = db.Column(db.Date)
    imagen = db.Column(db.LargeBinary)
    imagen_tipo = db.Column(db.String(50))
    moneda_rel = db.relationship('Moneda', foreign_keys=[moneda_cod])
    transacciones = db.relationship('Transaccion', backref='propiedad', lazy=True)


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
        moneda_str = request.form.get('moneda_cod', '').strip()

        p = Propiedad(
            direccion=request.form['direccion'].strip()[:50],
            disponible=request.form.get('disponible') == '1',
            costo_compra=Decimal(costo_str) if costo_str else None,
            moneda_cod=int(moneda_str) if moneda_str else None,
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


@app.route('/config/propiedad/edit/<int:cod>', methods=['POST'])
@login_required
def config_propiedad_edit(cod):
    p = db.session.get(Propiedad, cod)
    if not p:
        flash('Propiedad no encontrada.', 'warning')
        return redirect(url_for('configuracion') + '#propiedades')
    try:
        p.direccion = request.form['direccion'].strip()[:50]
        p.disponible = request.form.get('disponible') == '1'
        costo_str = request.form.get('costo_compra', '').strip()
        p.costo_compra = Decimal(costo_str) if costo_str else None
        moneda_str = request.form.get('moneda_cod', '').strip()
        p.moneda_cod = int(moneda_str) if moneda_str else None
        fecha_str = request.form.get('fecha_compra', '').strip()
        p.fecha_compra = datetime.strptime(fecha_str, '%Y-%m-%d').date() if fecha_str else None
        if 'imagen' in request.files:
            f = request.files['imagen']
            if f and f.filename:
                p.imagen = f.read()
                p.imagen_tipo = f.content_type
        db.session.commit()
        flash('Propiedad actualizada correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al actualizar: {e}', 'danger')
    return redirect(url_for('configuracion') + '#propiedades')


@app.route('/config/propiedad/delete/<int:cod>', methods=['POST'])
@login_required
def config_propiedad_delete(cod):
    p = db.session.get(Propiedad, cod)
    if not p:
        flash('Propiedad no encontrada.', 'warning')
        return redirect(url_for('configuracion') + '#propiedades')
    count = Transaccion.query.filter_by(propiedad_cod=cod).count()
    if count:
        flash(f'No se puede eliminar: la propiedad tiene {count} transacción/es asociada/s. '
              f'Eliminá primero esas transacciones.', 'danger')
        return redirect(url_for('configuracion') + '#propiedades')
    try:
        db.session.delete(p)
        db.session.commit()
        flash('Propiedad eliminada correctamente.', 'success')
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


@app.route('/config/concepto/edit/<int:cod>', methods=['POST'])
@login_required
def config_concepto_edit(cod):
    c = db.session.get(Concepto, cod)
    if not c:
        flash('Concepto no encontrado.', 'warning')
        return redirect(url_for('configuracion') + '#conceptos')
    try:
        c.nombre = request.form['nombre'].strip()[:30]
        c.tipo = request.form['tipo']
        db.session.commit()
        flash('Concepto actualizado correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al actualizar: {e}', 'danger')
    return redirect(url_for('configuracion') + '#conceptos')


@app.route('/config/concepto/delete/<int:cod>', methods=['POST'])
@login_required
def config_concepto_delete(cod):
    c = db.session.get(Concepto, cod)
    if not c:
        flash('Concepto no encontrado.', 'warning')
        return redirect(url_for('configuracion') + '#conceptos')
    count = Transaccion.query.filter_by(concepto_cod=cod).count()
    if count:
        flash(f'No se puede eliminar: el concepto "{c.nombre}" está usado en {count} transacción/es. '
              f'Modificá o eliminá esas transacciones primero.', 'danger')
        return redirect(url_for('configuracion') + '#conceptos')
    try:
        db.session.delete(c)
        db.session.commit()
        flash('Concepto eliminado correctamente.', 'success')
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


@app.route('/config/moneda/edit/<int:cod>', methods=['POST'])
@login_required
def config_moneda_edit(cod):
    m = db.session.get(Moneda, cod)
    if not m:
        flash('Moneda no encontrada.', 'warning')
        return redirect(url_for('configuracion') + '#monedas')
    try:
        m.nombre = request.form['nombre'].strip()[:30]
        m.signo = request.form['signo'].strip()[:3]
        db.session.commit()
        flash('Moneda actualizada correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al actualizar: {e}', 'danger')
    return redirect(url_for('configuracion') + '#monedas')


@app.route('/config/moneda/delete/<int:cod>', methods=['POST'])
@login_required
def config_moneda_delete(cod):
    m = db.session.get(Moneda, cod)
    if not m:
        flash('Moneda no encontrada.', 'warning')
        return redirect(url_for('configuracion') + '#monedas')
    count = Transaccion.query.filter_by(moneda_cod=cod).count()
    if count:
        flash(f'No se puede eliminar: la moneda "{m.nombre}" está usada en {count} transacción/es. '
              f'Modificá o eliminá esas transacciones primero.', 'danger')
        return redirect(url_for('configuracion') + '#monedas')
    try:
        db.session.delete(m)
        db.session.commit()
        flash('Moneda eliminada correctamente.', 'success')
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


@app.route('/config/usuario/edit/<int:cod>', methods=['POST'])
@login_required
def config_usuario_edit(cod):
    u = db.session.get(Usuario, cod)
    if not u:
        flash('Usuario no encontrado.', 'warning')
        return redirect(url_for('configuracion') + '#usuarios')
    try:
        nueva_pwd = request.form.get('password', '').strip()
        if not nueva_pwd or len(nueva_pwd) < 6:
            flash('La contraseña debe tener al menos 6 caracteres.', 'warning')
            return redirect(url_for('configuracion') + '#usuarios')
        u.password = generate_password_hash(nueva_pwd)
        db.session.commit()
        flash(f'Contraseña de "{u.nombre_usuario}" actualizada correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al actualizar: {e}', 'danger')
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
        return redirect(url_for('configuracion') + '#usuarios')
    try:
        db.session.delete(u)
        db.session.commit()
        flash('Usuario eliminado correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('configuracion') + '#usuarios')


# ─── Carga masiva ─────────────────────────────────────────────────────────────

def _parse_fecha(valor: str):
    for fmt in FORMATOS_FECHA:
        try:
            return datetime.strptime(valor.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def _parse_int(valor) -> int | None:
    try:
        return int(float(str(valor).strip()))
    except (ValueError, TypeError):
        return None


def _parse_decimal(valor) -> Decimal | None:
    try:
        return Decimal(str(valor).strip().replace(',', '.'))
    except (InvalidOperation, TypeError, AttributeError):
        return None


def _leer_archivo(f) -> tuple[list[dict], str | None]:
    """Lee CSV o Excel y devuelve (filas_como_dicts, error_o_None)."""
    nombre = f.filename.lower()
    if nombre.endswith('.csv'):
        try:
            contenido = f.read().decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(contenido))
            filas = [{k.strip().lower(): (v or '').strip() for k, v in row.items()}
                     for row in reader]
            return filas, None
        except Exception as e:
            return [], f'Error al leer CSV: {e}'
    elif nombre.endswith(('.xlsx', '.xls')):
        if not PANDAS_OK:
            return [], 'Para Excel instalá pandas y openpyxl. Alternatively usá CSV.'
        try:
            df = pd.read_excel(f, dtype=str)
            df.columns = [c.strip().lower() for c in df.columns]
            df = df.fillna('')
            filas = [{k: str(v).strip() for k, v in row.items()} for row in df.to_dict('records')]
            return filas, None
        except Exception as e:
            return [], f'Error al leer Excel: {e}'
    else:
        return [], 'Formato no válido. Usá .csv, .xlsx o .xls'


def _validar_filas(filas_raw: list[dict]) -> list[dict]:
    """Valida cada fila con controles a→b→c→d y devuelve lista enriquecida."""
    propiedades = {p.cod: p for p in Propiedad.query.all()}
    conceptos   = {c.cod: c for c in Concepto.query.all()}
    monedas     = {m.cod: m for m in Moneda.query.all()}

    resultado = []
    vistos = set()  # para duplicados intra-archivo

    for i, fila in enumerate(filas_raw, start=1):
        errores: list[str] = []
        categoria_error: str | None = None

        propiedad_cod = _parse_int(fila.get('propiedad_cod', ''))
        concepto_cod  = _parse_int(fila.get('concepto_cod', ''))
        moneda_cod    = _parse_int(fila.get('moneda_cod', ''))
        importe       = _parse_decimal(fila.get('importe', ''))
        fecha         = _parse_fecha(fila.get('fecha', ''))
        observaciones = fila.get('observaciones', '')[:50]

        propiedad_obj = concepto_obj = moneda_obj = None

        # ── (b) Validación de FK ──────────────────────────────────────────────
        if propiedad_cod is not None and propiedad_cod not in propiedades:
            errores.append(f'propiedad_cod {propiedad_cod} no existe')
            categoria_error = 'b'
        elif propiedad_cod:
            propiedad_obj = propiedades[propiedad_cod]

        if concepto_cod is not None and concepto_cod not in conceptos:
            errores.append(f'concepto_cod {concepto_cod} no existe')
            categoria_error = categoria_error or 'b'
        elif concepto_cod:
            concepto_obj = conceptos[concepto_cod]

        if moneda_cod is not None and moneda_cod not in monedas:
            errores.append(f'moneda_cod {moneda_cod} no existe')
            categoria_error = categoria_error or 'b'
        elif moneda_cod:
            moneda_obj = monedas[moneda_cod]

        # ── (c) Campos requeridos no vacíos ──────────────────────────────────
        if not categoria_error:
            if not fila.get('fecha', '').strip() or fecha is None:
                errores.append(f'fecha inválida o vacía (valor: "{fila.get("fecha","")}" — formato requerido: DD/MM/AAAA)')
                categoria_error = 'c'
            if propiedad_cod is None:
                errores.append('propiedad_cod vacío o inválido')
                categoria_error = categoria_error or 'c'
            if concepto_cod is None:
                errores.append('concepto_cod vacío o inválido')
                categoria_error = categoria_error or 'c'
            if importe is None:
                errores.append('importe inválido o vacío')
                categoria_error = categoria_error or 'c'
            if moneda_cod is None:
                errores.append('moneda_cod vacío o inválido')
                categoria_error = categoria_error or 'c'

        # ── (d) Duplicados ────────────────────────────────────────────────────
        if not categoria_error:
            clave = (str(fecha), propiedad_cod, concepto_cod, str(importe), moneda_cod)

            if clave in vistos:
                errores.append('registro duplicado dentro del archivo')
                categoria_error = 'd'
            else:
                vistos.add(clave)
                existe_db = Transaccion.query.filter_by(
                    fecha=fecha,
                    propiedad_cod=propiedad_cod,
                    concepto_cod=concepto_cod,
                    importe=importe,
                    moneda_cod=moneda_cod
                ).first()
                if existe_db:
                    errores.append('registro ya existe en la base de datos')
                    categoria_error = 'd'

        resultado.append({
            'fila': i,
            'fecha': str(fecha) if fecha else fila.get('fecha', ''),
            'propiedad_cod': propiedad_cod,
            'propiedad_dir': propiedad_obj.direccion if propiedad_obj else fila.get('propiedad_cod', ''),
            'concepto_cod': concepto_cod,
            'concepto_nombre': concepto_obj.nombre if concepto_obj else fila.get('concepto_cod', ''),
            'concepto_tipo': concepto_obj.tipo if concepto_obj else '',
            'importe': str(importe) if importe else fila.get('importe', ''),
            'moneda_cod': moneda_cod,
            'moneda_signo': moneda_obj.signo if moneda_obj else fila.get('moneda_cod', ''),
            'observaciones': observaciones,
            'errores': errores,
            'categoria_error': categoria_error,
            'valido': len(errores) == 0,
        })

    return resultado


@app.route('/carga/upload', methods=['POST'])
@login_required
def carga_upload():
    if 'archivo' not in request.files or not request.files['archivo'].filename:
        flash('No se seleccionó ningún archivo.', 'warning')
        return redirect(url_for('carga'))

    filas_raw, error = _leer_archivo(request.files['archivo'])
    if error:
        flash(error, 'danger')
        return redirect(url_for('carga'))

    if not filas_raw:
        flash('El archivo está vacío.', 'warning')
        return redirect(url_for('carga'))

    # (a) Verificar columnas requeridas
    columnas_presentes = set(filas_raw[0].keys())
    faltantes = COLUMNAS_REQUERIDAS - columnas_presentes
    if faltantes:
        flash(f'El archivo no tiene las columnas requeridas: {", ".join(sorted(faltantes))}', 'danger')
        return redirect(url_for('carga'))

    filas = _validar_filas(filas_raw)

    token = str(uuid.uuid4())
    _pending_imports[token] = filas
    session['import_token'] = token
    return redirect(url_for('carga_preview'))


@app.route('/carga/preview')
@login_required
def carga_preview():
    token = session.get('import_token')
    if not token or token not in _pending_imports:
        flash('No hay datos pendientes. Subí el archivo nuevamente.', 'warning')
        return redirect(url_for('carga'))
    filas = _pending_imports[token]
    return render_template('carga_preview.html', filas=filas)


@app.route('/carga/confirmar', methods=['POST'])
@login_required
def carga_confirmar():
    token = session.get('import_token')
    if not token or token not in _pending_imports:
        flash('Sesión expirada. Subí el archivo nuevamente.', 'warning')
        return redirect(url_for('carga'))

    todas = _pending_imports[token]
    seleccionadas = set(request.form.getlist('seleccionado'))

    insertados = errores_insert = 0
    for fila in todas:
        if str(fila['fila']) not in seleccionadas:
            continue
        if not fila['valido']:
            continue
        try:
            t = Transaccion(
                fecha=datetime.strptime(fila['fecha'], '%Y-%m-%d').date(),
                propiedad_cod=fila['propiedad_cod'],
                concepto_cod=fila['concepto_cod'],
                importe=Decimal(fila['importe']),
                moneda_cod=fila['moneda_cod'],
                observaciones=fila.get('observaciones', '')
            )
            db.session.add(t)
            insertados += 1
        except Exception:
            db.session.rollback()
            errores_insert += 1

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f'Error al guardar en la base de datos: {e}', 'danger')
        return redirect(url_for('carga_preview'))

    del _pending_imports[token]
    session.pop('import_token', None)

    flash(f'{insertados} transacciones cargadas correctamente.', 'success')
    if errores_insert:
        flash(f'{errores_insert} registros no pudieron insertarse.', 'warning')
    return redirect(url_for('carga'))


@app.route('/carga/template')
@login_required
def carga_template():
    contenido = ('fecha,propiedad_cod,concepto_cod,importe,moneda_cod,observaciones\n'
                 '15/01/2024,1,1,15000.00,1,Ejemplo de observacion\n'
                 '01/02/2024,1,2,5000.00,1,Otro ejemplo\n')
    return Response(
        contenido,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment;filename=plantilla_transacciones.csv'}
    )


# ─── Inicialización ───────────────────────────────────────────────────────────

def init_db():
    db.create_all()
    # Migración: agregar moneda_cod a propiedades si no existe (DBs pre-existentes)
    try:
        with db.engine.connect() as conn:
            conn.execute(text('ALTER TABLE propiedades ADD COLUMN moneda_cod INTEGER REFERENCES monedas(cod)'))
            conn.commit()
    except Exception:
        pass  # La columna ya existe
    # Usuario admin por defecto si no hay ninguno
    if not Usuario.query.first():
        admin = Usuario(
            nombre_usuario='ADMIN',
            password=generate_password_hash('1234')
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
