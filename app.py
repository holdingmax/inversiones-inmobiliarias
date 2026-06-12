import os
import uuid
import json
import csv
import io
from datetime import datetime, date, timedelta
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
_pending_mapper:  dict = {}

COLUMNAS_REQUERIDAS = {'fecha', 'importe', 'moneda_cod'}  # mínimo para leer el archivo
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
    notas = db.Column(db.String(250))
    adjunto_notas = db.Column(db.LargeBinary)
    adjunto_notas_tipo = db.Column(db.String(50))
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
    auditado = db.Column(db.Boolean, default=True, nullable=False)
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

_PAGE_SIZE = 30


def _paginas_visibles(pagina, total, delta=2):
    """Devuelve lista de números de página con None donde va el '…'."""
    pages = set()
    pages.add(1)
    pages.add(total)
    for p in range(max(1, pagina - delta), min(total + 1, pagina + delta + 1)):
        pages.add(p)
    result, prev = [], 0
    for p in sorted(pages):
        if p - prev > 1:
            result.append(None)
        result.append(p)
        prev = p
    return result


@app.route('/propiedad')
@login_required
def propiedad():
    propiedades = Propiedad.query.order_by(Propiedad.direccion).all()
    prop_sel       = None
    transacciones  = []
    total_ingresos = Decimal('0')
    total_egresos  = Decimal('0')
    conceptos      = Concepto.query.order_by(Concepto.nombre).all()
    total_paginas  = 1
    total_tx       = 0

    cod          = request.args.get('cod', type=int)
    filtro_audit = request.args.get('auditado', 'todos')
    filtro_obs   = request.args.get('obs', '').strip()
    pagina       = request.args.get('pagina', 1, type=int)

    if cod:
        prop_sel = db.session.get(Propiedad, cod)
        if prop_sel:
            q = Transaccion.query.filter_by(propiedad_cod=cod)
            if filtro_audit == 'si':
                q = q.filter_by(auditado=True)
            elif filtro_audit == 'no':
                q = q.filter_by(auditado=False)
            if filtro_obs:
                q = q.filter(Transaccion.observaciones.ilike(f'%{filtro_obs}%'))
            q = q.order_by(Transaccion.fecha)

            todas = q.all()
            total_tx = len(todas)
            for t in todas:
                if t.concepto_rel.tipo == 'ingreso':
                    total_ingresos += t.importe
                else:
                    total_egresos += t.importe

            total_paginas = max(1, (total_tx + _PAGE_SIZE - 1) // _PAGE_SIZE)
            pagina = max(1, min(pagina, total_paginas))
            transacciones = todas[(pagina - 1) * _PAGE_SIZE: pagina * _PAGE_SIZE]

    return render_template('propiedad.html',
                           propiedades=propiedades,
                           prop_sel=prop_sel,
                           transacciones=transacciones,
                           total_ingresos=total_ingresos,
                           total_egresos=total_egresos,
                           total_neto=total_ingresos - total_egresos,
                           cod_sel=cod,
                           filtro_audit=filtro_audit,
                           filtro_obs=filtro_obs,
                           conceptos=conceptos,
                           pagina=pagina,
                           total_paginas=total_paginas,
                           total_tx=total_tx,
                           paginas_visibles=_paginas_visibles(pagina, total_paginas))


@app.route('/propiedad/imagen/<int:cod>')
@login_required
def propiedad_imagen(cod):
    prop = db.session.get(Propiedad, cod)
    if not prop or not prop.imagen:
        return '', 404
    return Response(prop.imagen, mimetype=prop.imagen_tipo or 'image/jpeg')


@app.route('/propiedad/adjunto_notas/<int:cod>')
@login_required
def propiedad_adjunto_notas(cod):
    prop = db.session.get(Propiedad, cod)
    if not prop or not prop.adjunto_notas:
        return '', 404
    return Response(prop.adjunto_notas, mimetype=prop.adjunto_notas_tipo or 'image/jpeg')


@app.route('/propiedad/transaccion/<int:tx_id>/toggle_auditado', methods=['POST'])
@login_required
def toggle_auditado(tx_id):
    t = db.session.get(Transaccion, tx_id)
    if not t:
        return '', 404
    t.auditado = not t.auditado
    db.session.commit()
    return redirect(url_for('propiedad', cod=t.propiedad_cod,
                            auditado=request.form.get('filtro_audit', 'todos')))


@app.route('/propiedad/auditar_grupo', methods=['POST'])
@login_required
def auditar_grupo():
    propiedad_cod = request.form.get('propiedad_cod', type=int)
    concepto_cod  = request.form.get('concepto_cod',  type=int)
    filtro_audit  = request.form.get('filtro_audit', 'todos')
    if propiedad_cod and concepto_cod:
        Transaccion.query.filter_by(
            propiedad_cod=propiedad_cod,
            concepto_cod=concepto_cod,
            auditado=False
        ).update({'auditado': True})
        db.session.commit()
    return redirect(url_for('propiedad', cod=propiedad_cod, auditado=filtro_audit))


# ─── Carga de información ─────────────────────────────────────────────────────

@app.route('/carga', methods=['GET', 'POST'])
@login_required
def carga():
    propiedades = Propiedad.query.order_by(Propiedad.direccion).all()
    conceptos = Concepto.query.order_by(Concepto.nombre).all()
    monedas = Moneda.query.order_by(Moneda.nombre).all()

    if request.method == 'POST':
        try:
            propiedad_cod = int(request.form.get('propiedad_cod', 0) or 0)
            moneda_cod    = request.form.get('moneda_cod', type=int)
            if not moneda_cod and propiedad_cod:
                _p = db.session.get(Propiedad, propiedad_cod)
                moneda_cod = _p.moneda_cod if _p else None
            if not moneda_cod:
                raise ValueError('Seleccioná una moneda.')
            prop = db.session.get(Propiedad, propiedad_cod)
            if prop and prop.moneda_cod and prop.moneda_cod != moneda_cod:
                moneda_prop = Moneda.query.get(prop.moneda_cod)
                flash(f'La propiedad "{prop.direccion}" está configurada en '
                      f'{moneda_prop.nombre if moneda_prop else prop.moneda_cod}. '
                      f'La moneda de la transacción debe coincidir.', 'danger')
            else:
                t = Transaccion(
                    fecha=datetime.strptime(request.form['fecha'], '%Y-%m-%d').date(),
                    propiedad_cod=propiedad_cod,
                    concepto_cod=int(request.form['concepto_cod']),
                    importe=Decimal(request.form['importe']),
                    moneda_cod=moneda_cod,
                    observaciones=request.form.get('observaciones', '').strip()[:50],
                    auditado=True,
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

def _make_cusip(direccion: str) -> str:
    """Primeros 9 caracteres no-espacio de la dirección."""
    return direccion.replace(' ', '')[:9]


@app.route('/reportes')
@login_required
def reportes():
    import csv as csv_mod
    from collections import defaultdict
    monedas     = Moneda.query.order_by(Moneda.cod).all()
    propiedades = Propiedad.query.order_by(Propiedad.cod).all()
    tab         = request.args.get('tab', 'ing_gast')

    # ── Tab 1: Ingresos y Gastos ──────────────────────────────────────────────
    moneda_cod    = request.args.get('moneda_cod', type=int)
    propiedad_arg = request.args.get('propiedad_cod', 'consolidado')
    periodo       = request.args.get('periodo', 'resumen')
    sub_periodo   = request.args.get('sub_periodo', '')   # e.g. '1','2','3','4'
    anio_periodo  = request.args.get('anio_periodo', type=int) or date.today().year
    moneda_sel    = None
    propiedad_sel = None
    resultado     = None

    # Propiedades configuradas con la moneda seleccionada (para el dropdown del Tab 1)
    if moneda_cod:
        propiedades_filtradas = [p for p in propiedades if p.moneda_cod == moneda_cod]
    else:
        propiedades_filtradas = propiedades

    if moneda_cod:
        moneda_sel = Moneda.query.get(moneda_cod)
        if not moneda_sel:
            flash('Moneda no encontrada.', 'warning')
            return redirect(url_for('reportes'))

        query = Transaccion.query.filter_by(moneda_cod=moneda_cod)

        if propiedad_arg and propiedad_arg != 'consolidado':
            try:
                pcod = int(propiedad_arg)
                query = query.filter_by(propiedad_cod=pcod)
                propiedad_sel = Propiedad.query.get(pcod)
            except (ValueError, TypeError):
                propiedad_arg = 'consolidado'

        hoy = date.today()
        if periodo == '6meses':
            query = query.filter(Transaccion.fecha >= hoy - timedelta(days=180))
        elif periodo == 'cuatrimestral' and sub_periodo:
            n = int(sub_periodo)
            # 1Q=Ene-Abr  2Q=May-Ago  3Q=Sep-Dic
            starts = {1: (1, 1), 2: (5, 1), 3: (9, 1)}
            ends   = {1: (4, 30), 2: (8, 31), 3: (12, 31)}
            if n in starts:
                d_ini = date(anio_periodo, starts[n][0], starts[n][1])
                d_fin = date(anio_periodo, ends[n][0],   ends[n][1])
                query = query.filter(Transaccion.fecha >= d_ini,
                                     Transaccion.fecha <= d_fin)
        elif periodo == 'trimestral' and sub_periodo:
            n = int(sub_periodo)
            # 1T=Ene-Mar  2T=Abr-Jun  3T=Jul-Sep  4T=Oct-Dic
            starts = {1: (1, 1), 2: (4, 1), 3: (7, 1), 4: (10, 1)}
            ends   = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
            if n in starts:
                d_ini = date(anio_periodo, starts[n][0], starts[n][1])
                d_fin = date(anio_periodo, ends[n][0],   ends[n][1])
                query = query.filter(Transaccion.fecha >= d_ini,
                                     Transaccion.fecha <= d_fin)

        totales = defaultdict(Decimal)
        info_concepto = {}
        for t in query.all():
            c = t.concepto_rel
            totales[c.cod] += t.importe
            if c.cod not in info_concepto:
                info_concepto[c.cod] = {'nombre': c.nombre, 'tipo': c.tipo}

        ingresos, egresos = [], []
        total_ing = total_egr = Decimal('0')
        for cod, total in totales.items():
            item = {'nombre': info_concepto[cod]['nombre'], 'total': total}
            if info_concepto[cod]['tipo'] == 'ingreso':
                ingresos.append(item)
                total_ing += total
            else:
                egresos.append(item)
                total_egr += total

        ingresos.sort(key=lambda x: x['nombre'])
        egresos.sort(key=lambda x: x['nombre'])

        resultado = {
            'ingresos': ingresos,
            'egresos': egresos,
            'total_ingresos': total_ing,
            'total_egresos': total_egr,
            'rentabilidad': total_ing - total_egr,
        }

    # ── Tab 2: TIR ────────────────────────────────────────────────────────────
    tir_moneda_cod  = request.args.get('tir_moneda', type=int)
    tir_propiedad   = request.args.get('tir_propiedad', 'todas')
    tir_moneda_sel  = None
    tir_items       = []

    if tir_moneda_cod:
        tir_moneda_sel = Moneda.query.get(tir_moneda_cod)
        prop_ids = set()
        for row in db.session.query(Transaccion.propiedad_cod).filter_by(moneda_cod=tir_moneda_cod).distinct():
            prop_ids.add(row[0])
        for p in Propiedad.query.filter_by(moneda_cod=tir_moneda_cod).all():
            prop_ids.add(p.cod)

        if tir_propiedad != 'todas':
            try:
                prop_ids = {int(tir_propiedad)} & prop_ids
            except (ValueError, TypeError):
                tir_propiedad = 'todas'

        for pcod in sorted(prop_ids):
            p = Propiedad.query.get(pcod)
            if not p:
                continue
            cant_tx = Transaccion.query.filter_by(propiedad_cod=p.cod, moneda_cod=tir_moneda_cod).count()
            tir_items.append({
                'propiedad': p,
                'cusip': _make_cusip(p.direccion),
                'cant_tx': cant_tx,
                'tiene_inversion': bool(p.costo_compra and p.fecha_compra and p.moneda_cod == tir_moneda_cod),
            })

    return render_template('reportes.html',
                           monedas=monedas,
                           propiedades=propiedades,
                           propiedades_filtradas=propiedades_filtradas,
                           tab=tab,
                           # tab 1
                           moneda_cod=moneda_cod,
                           moneda_sel=moneda_sel,
                           propiedad_arg=propiedad_arg,
                           propiedad_sel=propiedad_sel,
                           periodo=periodo,
                           sub_periodo=sub_periodo,
                           anio_periodo=anio_periodo,
                           resultado=resultado,
                           # tab 2
                           tir_moneda_cod=tir_moneda_cod,
                           tir_moneda_sel=tir_moneda_sel,
                           tir_propiedad=tir_propiedad,
                           tir_items=tir_items,
                           hoy=date.today().strftime('%Y-%m-%d'))


@app.route('/reportes/tir/download', methods=['POST'])
@login_required
def tir_download():
    import csv as csv_mod

    moneda_cod    = request.form.get('moneda_cod', type=int)
    propiedad_arg = request.form.get('propiedad_cod', 'todas')
    tipo_archivo  = request.form.get('archivo', 'movimientos')
    fecha_val_str = request.form.get('fecha_valor', date.today().strftime('%Y-%m-%d'))

    if not moneda_cod:
        flash('Seleccioná una moneda.', 'warning')
        return redirect(url_for('reportes') + '?tab=tir')

    prop_query = Propiedad.query.order_by(Propiedad.cod)
    if propiedad_arg != 'todas':
        try:
            prop_query = prop_query.filter_by(cod=int(propiedad_arg))
        except (ValueError, TypeError):
            pass
    propiedades_sel = prop_query.all()

    if tipo_archivo == 'movimientos':
        rows = []
        for p in propiedades_sel:
            cusip = _make_cusip(p.direccion)
            if p.costo_compra and p.fecha_compra and p.moneda_cod == moneda_cod:
                rows.append({
                    'Date': p.fecha_compra.strftime('%Y-%m-%d'),
                    'Cusip': cusip,
                    'Net Amount': float(-p.costo_compra),
                    'Security ID': 'Inversion',
                })
            for t in (Transaccion.query
                      .filter_by(propiedad_cod=p.cod, moneda_cod=moneda_cod)
                      .order_by(Transaccion.fecha).all()):
                signo = 1 if t.concepto_rel.tipo == 'ingreso' else -1
                rows.append({
                    'Date': t.fecha.strftime('%Y-%m-%d'),
                    'Cusip': cusip,
                    'Net Amount': float(signo * t.importe),
                    'Security ID': 'Dividendo',
                })
        rows.sort(key=lambda r: (r['Cusip'], r['Date']))

        out = io.StringIO()
        w = csv_mod.DictWriter(out, fieldnames=['Date', 'Cusip', 'Net Amount', 'Security ID'])
        w.writeheader()
        w.writerows(rows)
        return Response(out.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment;filename=movimientos_tir.csv'})

    else:  # valores finales
        try:
            fecha_val = datetime.strptime(fecha_val_str, '%Y-%m-%d').strftime('%Y-%m-%d')
        except ValueError:
            fecha_val = date.today().strftime('%Y-%m-%d')

        rows = []
        for p in propiedades_sel:
            if p.costo_compra and p.moneda_cod == moneda_cod:
                cusip = _make_cusip(p.direccion)
                rows.append({
                    'Date': fecha_val,
                    'Cusip': cusip,
                    'Net Amount': float(p.costo_compra),
                    'Description': cusip,
                })

        out = io.StringIO()
        w = csv_mod.DictWriter(out, fieldnames=['Date', 'Cusip', 'Net Amount', 'Description'])
        w.writeheader()
        w.writerows(rows)
        return Response(out.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment;filename=valores_finales_tir.csv'})


# ─── Configuración ────────────────────────────────────────────────────────────

@app.route('/configuracion')
@login_required
def configuracion():
    return render_template('configuracion.html',
                           propiedades=Propiedad.query.order_by(Propiedad.cod).all(),
                           conceptos=Concepto.query.order_by(Concepto.cod).all(),
                           monedas=Moneda.query.order_by(Moneda.cod).all(),
                           usuarios=Usuario.query.order_by(Usuario.cod).all())


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

        adjunto_data = adjunto_tipo = None
        if 'adjunto_notas' in request.files:
            fa = request.files['adjunto_notas']
            if fa and fa.filename:
                adjunto_data = fa.read()
                adjunto_tipo = fa.content_type

        p = Propiedad(
            direccion=request.form['direccion'].strip()[:50],
            disponible=request.form.get('disponible') == '1',
            costo_compra=Decimal(costo_str) if costo_str else None,
            moneda_cod=int(moneda_str) if moneda_str else None,
            fecha_compra=datetime.strptime(fecha_str, '%Y-%m-%d').date() if fecha_str else None,
            imagen=imagen_data,
            imagen_tipo=imagen_tipo,
            notas=request.form.get('notas', '').strip()[:250] or None,
            adjunto_notas=adjunto_data,
            adjunto_notas_tipo=adjunto_tipo,
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
        p.notas = request.form.get('notas', '').strip()[:250] or None
        if 'adjunto_notas' in request.files:
            fa = request.files['adjunto_notas']
            if fa and fa.filename:
                p.adjunto_notas = fa.read()
                p.adjunto_notas_tipo = fa.content_type
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
            filas = [{k.strip().lower(): (v or '').strip()
                      for k, v in row.items() if k is not None}
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
            # Pandas convierte celdas de fecha de Excel a "YYYY-MM-DD HH:MM:SS".
            # Las normalizamos a DD/MM/AAAA para que pasen la validación.
            if 'fecha' in df.columns:
                def _normalizar_fecha_excel(v):
                    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
                        try:
                            return datetime.strptime(str(v).strip(), fmt).strftime('%d/%m/%Y')
                        except ValueError:
                            pass
                    return v
                df['fecha'] = df['fecha'].apply(_normalizar_fecha_excel)
            filas = [{k: str(v).strip() for k, v in row.items()} for row in df.to_dict('records')]
            return filas, None
        except Exception as e:
            return [], f'Error al leer Excel: {e}'
    else:
        return [], 'Formato no válido. Usá .csv, .xlsx o .xls'


def _validar_filas(filas_raw: list[dict]) -> list[dict]:
    """
    Valida cada fila. Solo fecha/importe/moneda son errores duros (bloquean la fila).
    propiedad_cod y concepto_cod son blandos: si faltan o son inválidos se marcan en
    necesita_completar para que el usuario los elija en la pantalla de revisión.
    """
    propiedades = {p.cod: p for p in Propiedad.query.all()}
    conceptos   = {c.cod: c for c in Concepto.query.all()}
    monedas     = {m.cod: m for m in Moneda.query.all()}

    resultado = []
    vistos = set()

    for i, fila in enumerate(filas_raw, start=1):
        errores_duros: list[str] = []       # impiden seleccionar la fila
        necesita_completar: list[str] = []  # el usuario completa en pantalla

        propiedad_cod = _parse_int(fila.get('propiedad_cod', ''))
        concepto_cod  = _parse_int(fila.get('concepto_cod', ''))
        moneda_cod    = _parse_int(fila.get('moneda_cod', ''))
        importe       = _parse_decimal(fila.get('importe', ''))
        fecha         = _parse_fecha(fila.get('fecha', ''))
        observaciones = fila.get('observaciones', '')[:50]

        propiedad_obj = concepto_obj = moneda_obj = None

        # ── Campos duros: fecha, importe, moneda ─────────────────────────────
        if not fila.get('fecha', '').strip() or fecha is None:
            errores_duros.append(
                f'Fecha inválida o vacía (valor: "{fila.get("fecha","")}" — formato requerido: DD/MM/AAAA)'
            )
        if importe is None:
            errores_duros.append('Importe inválido o vacío')
        if not fila.get('moneda_cod', '').strip():
            errores_duros.append('Moneda vacía')
        elif moneda_cod is None:
            errores_duros.append(f'Moneda inválida (valor: "{fila.get("moneda_cod","")}")')
        elif moneda_cod not in monedas:
            errores_duros.append(f'moneda_cod {moneda_cod} no existe en la tabla Monedas')
        else:
            moneda_obj = monedas[moneda_cod]

        # ── Campos blandos: propiedad, concepto ──────────────────────────────
        raw_prop = fila.get('propiedad_cod', '').strip()
        if not raw_prop or propiedad_cod is None or propiedad_cod not in propiedades:
            necesita_completar.append('propiedad_cod')
        else:
            propiedad_obj = propiedades[propiedad_cod]
            # Validar que la moneda de la transacción coincide con la de la propiedad
            if (not errores_duros and moneda_obj
                    and propiedad_obj.moneda_cod
                    and propiedad_obj.moneda_cod != moneda_cod):
                errores_duros.append(
                    f'La propiedad "{propiedad_obj.direccion}" está configurada en '
                    f'{monedas[propiedad_obj.moneda_cod].signo if propiedad_obj.moneda_cod in monedas else propiedad_obj.moneda_cod}, '
                    f'pero la transacción usa moneda_cod {moneda_cod}. Las monedas deben coincidir.'
                )

        raw_conc = fila.get('concepto_cod', '').strip()
        if not raw_conc or concepto_cod is None or concepto_cod not in conceptos:
            necesita_completar.append('concepto_cod')
        else:
            concepto_obj = conceptos[concepto_cod]

        # ── Duplicados (solo cuando todos los campos están disponibles) ───────
        todo_completo = (not errores_duros and not necesita_completar
                         and fecha and importe and moneda_cod and propiedad_cod and concepto_cod)
        if todo_completo:
            clave = (str(fecha), propiedad_cod, concepto_cod, str(importe), moneda_cod, observaciones)
            if clave in vistos:
                errores_duros.append('Registro duplicado dentro del archivo')
            else:
                vistos.add(clave)
                if Transaccion.query.filter_by(
                    fecha=fecha, propiedad_cod=propiedad_cod,
                    concepto_cod=concepto_cod, importe=importe,
                    moneda_cod=moneda_cod, observaciones=observaciones
                ).first():
                    errores_duros.append('El registro ya existe en la base de datos')

        resultado.append({
            'fila': i,
            'fecha': str(fecha) if fecha else fila.get('fecha', ''),
            'propiedad_cod': propiedad_obj.cod if propiedad_obj else None,
            'propiedad_dir': propiedad_obj.direccion if propiedad_obj else '',
            'concepto_cod': concepto_obj.cod if concepto_obj else None,
            'concepto_nombre': concepto_obj.nombre if concepto_obj else '',
            'concepto_tipo': concepto_obj.tipo if concepto_obj else '',
            'importe': str(importe) if importe else fila.get('importe', ''),
            'moneda_cod': moneda_cod,
            'moneda_signo': moneda_obj.signo if moneda_obj else '',
            'observaciones': observaciones,
            'errores': errores_duros,
            'necesita_completar': necesita_completar,
            'valido': len(errores_duros) == 0,
            'completo': len(errores_duros) == 0 and len(necesita_completar) == 0,
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
    propiedades = Propiedad.query.order_by(Propiedad.cod).all()
    conceptos   = Concepto.query.order_by(Concepto.cod).all()
    return render_template('carga_preview.html', filas=filas,
                           propiedades=propiedades, conceptos=conceptos)


@app.route('/carga/confirmar', methods=['POST'])
@login_required
def carga_confirmar():
    token = session.get('import_token')
    if not token or token not in _pending_imports:
        flash('Sesión expirada. Subí el archivo nuevamente.', 'warning')
        return redirect(url_for('carga'))

    todas = _pending_imports[token]
    seleccionadas = set(request.form.getlist('seleccionado'))

    def _get_cod(fila, campo):
        """Devuelve el cod del campo: del fila si ya estaba, sino del form."""
        if fila[campo] is not None:
            return fila[campo]
        raw = request.form.get(f'{campo}_{fila["fila"]}', '').strip()
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    # Verificar que todos los seleccionados estén completos y con moneda coherente
    incompletos = []
    for fila in todas:
        if str(fila['fila']) not in seleccionadas or not fila['valido']:
            continue
        prop_cod = _get_cod(fila, 'propiedad_cod')
        conc     = _get_cod(fila, 'concepto_cod')
        if not prop_cod or not conc:
            incompletos.append(str(fila['fila']))
            continue
        prop = db.session.get(Propiedad, prop_cod)
        if prop and prop.moneda_cod and prop.moneda_cod != fila['moneda_cod']:
            moneda_prop = Moneda.query.get(prop.moneda_cod)
            incompletos.append(
                f'{fila["fila"]} (moneda incorrecta: la propiedad usa '
                f'{moneda_prop.nombre if moneda_prop else prop.moneda_cod})'
            )

    if incompletos:
        flash(f'Completá todos los campos requeridos antes de guardar '
              f'(filas incompletas: {", ".join(incompletos)}).', 'warning')
        return redirect(url_for('carga_preview'))

    insertados = errores_insert = 0
    for fila in todas:
        if str(fila['fila']) not in seleccionadas or not fila['valido']:
            continue
        try:
            t = Transaccion(
                fecha=datetime.strptime(fila['fecha'], '%Y-%m-%d').date(),
                propiedad_cod=_get_cod(fila, 'propiedad_cod'),
                concepto_cod=_get_cod(fila, 'concepto_cod'),
                importe=Decimal(fila['importe']),
                moneda_cod=fila['moneda_cod'],
                observaciones=fila.get('observaciones', ''),
                auditado=False,
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


# ─── Gestión de transacciones desde Configuración ───────────────────────────

_TX_PAGE_SIZE = 30


@app.route('/config/transacciones')
@login_required
def config_transacciones():
    propiedades  = Propiedad.query.order_by(Propiedad.direccion).all()
    conceptos    = Concepto.query.order_by(Concepto.nombre).all()
    monedas      = Moneda.query.order_by(Moneda.nombre).all()
    prop_filtro  = request.args.get('prop', type=int)
    pagina       = request.args.get('pagina', 1, type=int)

    q = Transaccion.query
    if prop_filtro:
        q = q.filter_by(propiedad_cod=prop_filtro)
    q = q.order_by(Transaccion.fecha.desc(), Transaccion.id.desc())

    total_tx      = q.count()
    total_paginas = max(1, (total_tx + _TX_PAGE_SIZE - 1) // _TX_PAGE_SIZE)
    pagina        = max(1, min(pagina, total_paginas))
    transacciones = q.offset((pagina - 1) * _TX_PAGE_SIZE).limit(_TX_PAGE_SIZE).all()

    return render_template('config_transacciones.html',
                           propiedades=propiedades,
                           conceptos=conceptos,
                           monedas=monedas,
                           transacciones=transacciones,
                           prop_filtro=prop_filtro,
                           pagina=pagina,
                           total_paginas=total_paginas,
                           total_tx=total_tx,
                           paginas_visibles=_paginas_visibles(pagina, total_paginas))


@app.route('/config/transaccion/<int:tx_id>/editar', methods=['POST'])
@login_required
def config_transaccion_editar(tx_id):
    t = db.session.get(Transaccion, tx_id)
    if not t:
        flash('Transacción no encontrada.', 'danger')
        return redirect(url_for('config_transacciones'))
    try:
        t.fecha         = datetime.strptime(request.form['fecha'], '%Y-%m-%d').date()
        t.propiedad_cod = int(request.form['propiedad_cod'])
        t.concepto_cod  = int(request.form['concepto_cod'])
        t.importe       = Decimal(request.form['importe'])
        t.moneda_cod    = int(request.form['moneda_cod'])
        t.observaciones = request.form.get('observaciones', '').strip()[:50]
        db.session.commit()
        flash('Transacción actualizada correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al actualizar: {e}', 'danger')
    return redirect(url_for('config_transacciones',
                            prop=request.form.get('prop_filtro', ''),
                            pagina=request.form.get('pagina', 1)))


@app.route('/config/transaccion/<int:tx_id>/eliminar', methods=['POST'])
@login_required
def config_transaccion_eliminar(tx_id):
    t = db.session.get(Transaccion, tx_id)
    if not t:
        flash('Transacción no encontrada.', 'danger')
        return redirect(url_for('config_transacciones'))
    try:
        db.session.delete(t)
        db.session.commit()
        flash('Transacción eliminada correctamente.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al eliminar: {e}', 'danger')
    return redirect(url_for('config_transacciones',
                            prop=request.form.get('prop_filtro', ''),
                            pagina=request.form.get('pagina', 1)))


# ─── Preparador de archivos para carga masiva ────────────────────────────────

@app.route('/carga/preparar_upload', methods=['POST'])
@login_required
def carga_preparar_upload():
    if not PANDAS_OK:
        flash('pandas no disponible.', 'danger')
        return redirect(url_for('carga'))
    f = request.files.get('archivo_prep')
    if not f or not f.filename:
        flash('Seleccioná un archivo Excel.', 'danger')
        return redirect(url_for('carga'))
    nombre = f.filename.lower()
    try:
        if nombre.endswith(('.xlsx', '.xls')):
            df = pd.read_excel(f, dtype=str, header=0)
        elif nombre.endswith('.csv'):
            content = f.read().decode('utf-8-sig')
            df = pd.read_csv(io.StringIO(content), dtype=str, header=0)
        else:
            flash('Formato no soportado. Usá Excel o CSV.', 'danger')
            return redirect(url_for('carga'))
        df.columns = [str(c).strip() for c in df.columns]
        df = df.where(pd.notna(df), '')
        filas    = df.to_dict(orient='records')
        columnas = list(df.columns)
        token = str(uuid.uuid4())
        _pending_mapper[token] = {'filas': filas, 'columnas': columnas}
        return redirect(url_for('carga_preparar_mapeo', token=token))
    except Exception as e:
        flash(f'Error al leer el archivo: {e}', 'danger')
        return redirect(url_for('carga'))


@app.route('/carga/preparar_mapeo')
@login_required
def carga_preparar_mapeo():
    token = request.args.get('token', '')
    datos = _pending_mapper.get(token)
    if not datos:
        flash('Sesión expirada. Subí el archivo nuevamente.', 'warning')
        return redirect(url_for('carga'))
    return render_template('preparar_mapeo.html',
                           token=token,
                           columnas=datos['columnas'],
                           total_filas=len(datos['filas']),
                           propiedades=Propiedad.query.order_by(Propiedad.direccion).all(),
                           conceptos=Concepto.query.order_by(Concepto.nombre).all(),
                           monedas=Moneda.query.order_by(Moneda.nombre).all())


@app.route('/carga/preparar_procesar', methods=['POST'])
@login_required
def carga_preparar_procesar():
    token = request.form.get('token', '')
    datos = _pending_mapper.get(token)
    if not datos:
        flash('Sesión expirada. Subí el archivo nuevamente.', 'warning')
        return redirect(url_for('carga'))

    filas = datos['filas']

    map_fecha    = request.form.get('map_fecha', '')
    map_importe  = request.form.get('map_importe', '')
    map_obs      = request.form.get('map_observaciones', '')
    map_prop     = request.form.get('map_propiedad_cod', '')
    map_concepto = request.form.get('map_concepto_cod', '')
    map_moneda   = request.form.get('map_moneda_cod', '')

    global_prop     = request.form.get('global_propiedad_cod', '')
    global_concepto = request.form.get('global_concepto_cod', '')
    global_moneda   = request.form.get('global_moneda_cod', '')

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['fecha', 'propiedad_cod', 'concepto_cod', 'importe', 'moneda_cod', 'observaciones'])

    for fila in filas:
        fecha_val   = str(fila.get(map_fecha, '')   if map_fecha   else '').strip()
        obs_val     = str(fila.get(map_obs, '')     if map_obs     else '').strip()[:50]

        # importe: quitar signo negativo
        raw_imp = str(fila.get(map_importe, '') if map_importe else '').strip()
        try:
            importe_val = str(abs(float(raw_imp.replace(',', '.'))))
        except Exception:
            importe_val = raw_imp

        prop_val    = global_prop     or (str(fila.get(map_prop, '')     if map_prop     else '').strip())
        conc_val    = global_concepto or (str(fila.get(map_concepto, '') if map_concepto else '').strip())
        mon_val     = global_moneda   or (str(fila.get(map_moneda, '')   if map_moneda   else '').strip())

        writer.writerow([fecha_val, prop_val, conc_val, importe_val, mon_val, obs_val])

    _pending_mapper.pop(token, None)

    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=archivo_preparado.csv'}
    )


# ─── Inicialización ───────────────────────────────────────────────────────────

def init_db():
    db.create_all()
    # Migración: agregar moneda_cod a propiedades si no existe (DBs pre-existentes)
    for _sql in [
        'ALTER TABLE propiedades ADD COLUMN moneda_cod INTEGER REFERENCES monedas(cod)',
        'ALTER TABLE propiedades ADD COLUMN notas VARCHAR(250)',
        'ALTER TABLE propiedades ADD COLUMN adjunto_notas BLOB',
        'ALTER TABLE propiedades ADD COLUMN adjunto_notas_tipo VARCHAR(50)',
        'ALTER TABLE transacciones ADD COLUMN auditado BOOLEAN NOT NULL DEFAULT 1',
    ]:
        try:
            with db.engine.connect() as conn:
                conn.execute(text(_sql))
                conn.commit()
        except Exception:
            pass  # columna ya existe
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
