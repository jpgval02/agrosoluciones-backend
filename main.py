from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from supabase import create_client, Client
import os
from dotenv import load_dotenv

# --- NUEVAS IMPORTACIONES PARA GOOGLE CALENDAR ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, date

load_dotenv()
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(url, key)

app = FastAPI(title="API Operativa - Agrosoluciones Aéreas")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- WEBSOCKETS (TIEMPO REAL) ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                pass

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# --- MODELOS DE DATOS ---
class Credenciales(BaseModel):
    email: str
    password: str

class Verifica2FA(BaseModel):
    factor_id: str
    codigo: str
    email: str

# --- SISTEMA DE PERMISOS POR ROL ---
def obtener_usuario_actual(authorization: str = Header(None)):
    """Valida el token que manda el frontend y devuelve el correo + rol del usuario."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="No has iniciado sesión.")
    token = authorization.replace("Bearer ", "")
    try:
        usuario_resp = supabase.auth.get_user(token)
        email = usuario_resp.user.email
    except Exception:
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada. Vuelve a iniciar sesión.")

    rol = "operador"
    try:
        datos_rol = supabase.table('roles').select('rol').eq('email', email).execute()
        if len(datos_rol.data) > 0:
            rol = datos_rol.data[0]['rol']
    except Exception:
        pass

    return {"email": email, "rol": rol}


def requiere_rol(*roles_permitidos):
    """Úsalo como Depends(requiere_rol('admin')) o Depends(requiere_rol('admin', 'financiero'))."""
    def verificador(usuario: dict = Depends(obtener_usuario_actual)):
        if usuario["rol"] not in roles_permitidos:
            raise HTTPException(status_code=403, detail="No tienes permiso para realizar esta acción.")
        return usuario
    return verificador

class ClienteNuevo(BaseModel):
    nombre: str
    apellidos: str
    telefono: str
    rfc: str
    estado: Optional[str] = "Prospecto"
    no_cliente: Optional[str] = None

class PropiedadNueva(BaseModel):
    cliente_id: str  
    nombre_propiedad: str
    direccion: str
    estado: str
    tipo_cultivo: str
    superficie_has: float
    sistema_riego: str
    comentarios: str

class ServicioNuevo(BaseModel):
    propiedad_id: str 
    fecha_aplicacion: str
    costo_servicio: float
    estado_cuenta: str
    satisfaccion: int
    no_cotizacion: Optional[str] = None
    no_orden: Optional[str] = None
    gastos: Optional[float] = 0.0
    fecha_seguimiento: Optional[str] = None
    no_factura: Optional[str] = None
    observaciones: Optional[str] = None
    gasto_gasolina_unidad: Optional[float] = 0.0
    gasto_gasolina_generador: Optional[float] = 0.0
    gasto_sueldos: Optional[float] = 0.0
    gasto_insumos: Optional[float] = 0.0
    gasto_comidas: Optional[float] = 0.0
    gasto_oxxo: Optional[float] = 0.0
    ha_trabajadas: Optional[float] = 0.0
    precio_por_ha: Optional[float] = 0.0
    ingreso_viaticos: Optional[float] = 0.0
    ingreso_suministros: Optional[float] = 0.0
    metodo_pago: Optional[str] = "Efectivo"

class MetasMensuales(BaseModel):
    mes_anio: str
    meta_ventas: float
    meta_servicios: int
    meta_clientes: int
    meta_prospectos: int
    meta_prospectos_visitas: Optional[int] = 0
    visitas_reales: Optional[int] = 0

class SeguimientoHecho(BaseModel):
    fecha_completado: str

class CotizacionNueva(BaseModel):
    cliente_id: str
    fecha: str
    cultivo: str
    hectareas: float
    precio_ha: float
    total: float
    estado: Optional[str] = "Pendiente"
    observaciones: Optional[str] = ""


# --- FUNCIÓN DE GOOGLE CALENDAR (ACTUALIZADA PARA INVITADOS) ---
def agendar_en_google_calendar(fecha, no_cotizacion, observaciones, nombre_productor, parcela, hectareas):
    # ¡IMPORTANTE! Cambia esto por el correo dueño del calendario
    CORREO_CALENDARIO = 'facturacion@asoa.com.mx' 
    ARCHIVO_CREDENCIALES = 'credenciales_calendario.json'

    # --- AQUÍ PONES LOS CORREOS DE TU EQUIPO ---
    correos_equipo = [
        {'email': 'jpgval02@gmail.com'},
        {'email': 'tecnico@asoa.com.mx'},
        {'email': 'otro_companero@gmail.com'}
    ]

    # --- Solo agendamos si la fecha es hoy o a futuro ---
    try:
        fecha_evento = datetime.strptime(fecha, '%Y-%m-%d').date()
        if fecha_evento < date.today():
            print(f"Fecha {fecha} ya pasó, no se agenda en el calendario.")
            return None
    except ValueError:
        print(f"Fecha inválida para el calendario: {fecha}")
        return None

    if not os.path.exists(ARCHIVO_CREDENCIALES):
        print(f"No se encontró la llave del calendario en '{ARCHIVO_CREDENCIALES}'. Revisa el nombre exacto del archivo.")
        return None

    try:
        creds = service_account.Credentials.from_service_account_file(
            ARCHIVO_CREDENCIALES, scopes=['https://www.googleapis.com/auth/calendar']
        ).with_subject('facturacion@asoa.com.mx')
        servicio = build('calendar', 'v3', credentials=creds)

        evento = {
            'summary': f'🚜 Vuelo: {nombre_productor} ({hectareas} Ha.)',
            'location': parcela,
            'description': f'Cotización/OS: {no_cotizacion}\nHectáreas: {hectareas} Ha.\nObservaciones: {observaciones}',
            'start': {'date': fecha, 'timeZone': 'America/Mexico_City'},
            'end': {'date': fecha, 'timeZone': 'America/Mexico_City'},
        }

        # Primero intentamos con invitados (requiere Domain-Wide Delegation).
        # Si falla por permisos, creamos el evento sin invitados en vez de perderlo por completo.
        try:
            evento['attendees'] = correos_equipo
            servicio.events().insert(
                calendarId=CORREO_CALENDARIO,
                body=evento,
                sendUpdates='all'  # <-- ESTO LES AVISA POR CORREO
            ).execute()
            print("¡Cita agendada y equipo notificado con éxito!")
        except Exception as invite_err:
            if 'forbiddenForServiceAccounts' in str(invite_err) or 'Domain-Wide Delegation' in str(invite_err):
                print("No se pudo invitar al equipo (falta Domain-Wide Delegation). Se crea la cita sin invitados.")
                evento.pop('attendees', None)
                servicio.events().insert(calendarId=CORREO_CALENDARIO, body=evento).execute()
                print("¡Cita agendada sin invitados!")
            else:
                raise

    except Exception as e:
        print(f"Error con Google Calendar: {e}")


# --- RUTA DE LOGIN PRINCIPAL ---
@app.post("/login/")
async def iniciar_sesion(credenciales: Credenciales):
    try:
        respuesta = supabase.auth.sign_in_with_password({
            "email": credenciales.email,
            "password": credenciales.password
        })
        
        factores_info = supabase.auth.mfa.list_factors()
        factores = getattr(factores_info, 'all', []) if hasattr(factores_info, 'all') else factores_info.get('all', [])
        
        factores_verificados = []
        factores_sucios = []
        for f in factores:
            status = getattr(f, 'status', None) if hasattr(f, 'status') else f.get('status')
            if status == 'verified':
                factores_verificados.append(f)
            else:
                factores_sucios.append(f)
                
        if len(factores_verificados) > 0:
            f = factores_verificados[0]
            f_id = getattr(f, 'id', None) if hasattr(f, 'id') else f.get('id')
            return {
                "mensaje": "Requiere 2FA", 
                "necesita_2fa": True, 
                "tipo": "login",
                "factor_id": f_id
            }
        else:
            for fs in factores_sucios:
                fs_id = getattr(fs, 'id', None) if hasattr(fs, 'id') else fs.get('id')
                try: supabase.auth.mfa.unenroll({"factor_id": fs_id})
                except: pass

            enroll_res = supabase.auth.mfa.enroll({
                "factor_type": "totp",
                "issuer": "Sistema ASOA",
                "friendly_name": "Portal Operativo"
            })
            
            factor_id = getattr(enroll_res, 'id', None) if hasattr(enroll_res, 'id') else enroll_res.get('id')
            totp = getattr(enroll_res, 'totp', None) if hasattr(enroll_res, 'totp') else enroll_res.get('totp', {})
            qr_code = getattr(totp, 'qr_code', None) if hasattr(totp, 'qr_code') else totp.get('qr_code', '')
            
            return {
                "mensaje": "Requiere configurar 2FA",
                "necesita_2fa": True,
                "tipo": "setup",
                "factor_id": factor_id,
                "qr_code": qr_code
            }
            
    except Exception as e:
        error_msg = str(e)
        if "Invalid login credentials" in error_msg:
            raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos.")
        else:
            raise HTTPException(status_code=400, detail=f"Error Interno MFA: {error_msg}")

# --- RUTA VERIFICACIÓN 2 PASOS ---
@app.post("/verificar-2fa/")
async def verificar_2fa(req: Verifica2FA):
    try:
        challenge = supabase.auth.mfa.challenge({"factor_id": req.factor_id})
        ch_id = getattr(challenge, 'id', None) if hasattr(challenge, 'id') else challenge.get('id')
        
        verificacion = supabase.auth.mfa.verify({
            "factor_id": req.factor_id, 
            "challenge_id": ch_id, 
            "code": req.codigo
        })
        
        rol_usuario = "operador"
        try:
            datos_rol = supabase.table('roles').select('rol').eq('email', req.email).execute()
            if len(datos_rol.data) > 0:
                rol_usuario = datos_rol.data[0]['rol']
        except Exception:
            pass

        access_token = getattr(verificacion, 'access_token', None) if hasattr(verificacion, 'access_token') else verificacion.get('access_token')

        return {"mensaje": "Acceso concedido", "rol": rol_usuario, "access_token": access_token}
    except Exception as e:
        raise HTTPException(status_code=401, detail="Código 2FA incorrecto o expirado.")

# --- RUTAS DE CLIENTES ---
@app.post("/clientes/")
async def registrar_cliente(cliente: ClienteNuevo, usuario: dict = Depends(requiere_rol("admin", "operador"))):
    try:
        respuesta = supabase.table('clientes').insert(cliente.dict()).execute()
        await manager.broadcast("update") 
        return {"mensaje": "Guardado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/clientes/")
async def obtener_clientes(usuario: dict = Depends(obtener_usuario_actual)):
    respuesta = supabase.table('clientes').select("*").execute()
    return respuesta.data

@app.put("/clientes/{cliente_id}")
async def actualizar_cliente(cliente_id: str, cliente: ClienteNuevo, usuario: dict = Depends(requiere_rol("admin", "operador"))):
    try:
        respuesta = supabase.table('clientes').update(cliente.dict()).eq('id', cliente_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/clientes/{cliente_id}")
async def eliminar_cliente(cliente_id: str, usuario: dict = Depends(requiere_rol("admin"))):
    try:
        supabase.table('clientes').delete().eq('id', cliente_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Eliminado exitosamente"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- RUTAS DE PROPIEDADES ---
@app.post("/propiedades/")
async def registrar_propiedad(propiedad: PropiedadNueva, usuario: dict = Depends(requiere_rol("admin", "operador"))):
    try:
        respuesta = supabase.table('propiedades').insert(propiedad.dict()).execute()
        await manager.broadcast("update")
        return {"mensaje": "Guardado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/propiedades/")
async def obtener_propiedades(usuario: dict = Depends(obtener_usuario_actual)):
    respuesta = supabase.table('propiedades').select("*").execute()
    return respuesta.data

@app.put("/propiedades/{propiedad_id}")
async def actualizar_propiedad(propiedad_id: str, propiedad: PropiedadNueva, usuario: dict = Depends(requiere_rol("admin", "operador"))):
    try:
        respuesta = supabase.table('propiedades').update(propiedad.dict()).eq('id', propiedad_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/propiedades/{propiedad_id}")
async def eliminar_propiedad(propiedad_id: str, usuario: dict = Depends(requiere_rol("admin"))):
    try:
        supabase.table('propiedades').delete().eq('id', propiedad_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Eliminado exitosamente"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- RUTAS DE SERVICIOS (MODIFICADA CON GOOGLE CALENDAR) ---
CAMPOS_FINANCIEROS_SERVICIO = [
    'costo_servicio', 'gastos', 'gasto_gasolina_unidad', 'gasto_gasolina_generador',
    'gasto_sueldos', 'gasto_insumos', 'gasto_comidas', 'gasto_oxxo',
    'precio_por_ha', 'ingreso_viaticos', 'ingreso_suministros'
]

@app.post("/servicios/")
async def registrar_servicio(servicio: ServicioNuevo, usuario: dict = Depends(requiere_rol("admin", "operador"))):
    try:
        respuesta = supabase.table('servicios_aplicacion').insert(servicio.dict()).execute()
        
        # 1. Buscamos el nombre de la Parcela y el Cliente para el Calendario
        nombre_productor = "Productor Desconocido"
        nombre_parcela = "Ubicación Desconocida"
        
        try:
            prop_data = supabase.table('propiedades').select('cliente_id, nombre_propiedad, direccion').eq('id', servicio.propiedad_id).execute()
            if len(prop_data.data) > 0:
                cl_id = prop_data.data[0]['cliente_id']
                nombre_parcela = f"{prop_data.data[0]['nombre_propiedad']} ({prop_data.data[0]['direccion']})"
                
                # Actualizamos al cliente a "Cliente Real"
                supabase.table('clientes').update({'estado': 'Cliente'}).eq('id', cl_id).execute()
                
                # Sacamos los datos del cliente
                cliente_data = supabase.table('clientes').select('nombre, apellidos').eq('id', cl_id).execute()
                if len(cliente_data.data) > 0:
                    nombre_productor = f"{cliente_data.data[0]['nombre']} {cliente_data.data[0]['apellidos']}"
        except Exception as query_err:
            print(f"Error consultando detalles del cliente para el calendario: {query_err}")

        # 2. Refrescamos el panel de todos los usuarios
        await manager.broadcast("update")
        
        # 3. Disparamos la agenda a Google Calendar (usa la fecha real de la venta/servicio)
        fecha_cita = servicio.fecha_aplicacion
        if fecha_cita:
            agendar_en_google_calendar(
                fecha=fecha_cita,
                no_cotizacion=servicio.no_cotizacion or 'S/N',
                observaciones=servicio.observaciones or 'Sin detalles',
                nombre_productor=nombre_productor,
                parcela=nombre_parcela,
                hectareas=servicio.ha_trabajadas or 0
            )
            
        return {"mensaje": "Guardado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/servicios/")
async def obtener_servicios(usuario: dict = Depends(obtener_usuario_actual)):
    respuesta = supabase.table('servicios_aplicacion').select("*").execute()
    datos = respuesta.data
    if usuario["rol"] == "operador":
        for fila in datos:
            for campo in CAMPOS_FINANCIEROS_SERVICIO:
                fila.pop(campo, None)
    return datos

@app.put("/servicios/{servicio_id}")
async def actualizar_servicio(servicio_id: str, servicio: ServicioNuevo, usuario: dict = Depends(requiere_rol("admin", "operador"))):
    try:
        respuesta = supabase.table('servicios_aplicacion').update(servicio.dict()).eq('id', servicio_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/servicios/{servicio_id}")
async def eliminar_servicio(servicio_id: str, usuario: dict = Depends(requiere_rol("admin"))):
    try:
        supabase.table('servicios_aplicacion').delete().eq('id', servicio_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Eliminado exitosamente"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- SUBIR ENCUESTA DE SATISFACCIÓN (ARCHIVO) PARA UN SERVICIO ---
@app.post("/servicios/{servicio_id}/encuesta")
async def subir_encuesta(servicio_id: str, archivo: UploadFile = File(...), usuario: dict = Depends(obtener_usuario_actual)):
    try:
        contenido = await archivo.read()
        extension = archivo.filename.split('.')[-1] if '.' in archivo.filename else 'pdf'
        nombre_archivo = f"{servicio_id}.{extension}"

        # Sube el archivo al bucket 'encuestas' (upsert=true permite reemplazar si ya existía una)
        supabase.storage.from_('encuestas').upload(
            nombre_archivo,
            contenido,
            {"content-type": archivo.content_type, "upsert": "true"}
        )

        url_publica = supabase.storage.from_('encuestas').get_public_url(nombre_archivo)

        supabase.table('servicios_aplicacion').update({
            'url_encuesta': url_publica
        }).eq('id', servicio_id).execute()

        await manager.broadcast("update")
        return {"mensaje": "Encuesta subida con éxito", "url": url_publica}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- RUTA DE BOTON "HECHO" EN SEGUIMIENTOS ---
@app.post("/api/servicios/{servicio_id}/hecho")
async def marcar_seguimiento_hecho(servicio_id: str, req: SeguimientoHecho, usuario: dict = Depends(obtener_usuario_actual)):
    try:
        res = supabase.table('servicios_aplicacion').select('observaciones').eq('id', servicio_id).execute()
        obs_actual = ""
        if len(res.data) > 0:
            obs_actual = res.data[0].get('observaciones') or ""
            
        separador = "\n" if obs_actual else ""
        nueva_obs = f"{obs_actual}{separador}[Sistema] Seguimiento completado el {req.fecha_completado}."
        
        supabase.table('servicios_aplicacion').update({
            "fecha_seguimiento": None,
            "observaciones": nueva_obs
        }).eq('id', servicio_id).execute()
        
        await manager.broadcast("update")
        return {"mensaje": "Seguimiento completado"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- RUTAS DE COTIZACIONES ---
@app.post("/cotizaciones/")
async def registrar_cotizacion(cot: CotizacionNueva, usuario: dict = Depends(requiere_rol("admin", "operador"))):
    try:
        respuesta = supabase.table('cotizaciones').insert(cot.dict()).execute()
        await manager.broadcast("update")
        return {"mensaje": "Cotización guardada", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/cotizaciones/")
async def obtener_cotizaciones(usuario: dict = Depends(obtener_usuario_actual)):
    respuesta = supabase.table('cotizaciones').select("*").execute()
    return respuesta.data

@app.put("/cotizaciones/{cot_id}")
async def actualizar_cotizacion(cot_id: str, cot: CotizacionNueva, usuario: dict = Depends(requiere_rol("admin", "operador"))):
    try:
        respuesta = supabase.table('cotizaciones').update(cot.dict()).eq('id', cot_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/cotizaciones/{cot_id}")
async def eliminar_cotizacion(cot_id: str, usuario: dict = Depends(requiere_rol("admin"))):
    try:
        supabase.table('cotizaciones').delete().eq('id', cot_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Eliminado"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- VISITAS, METAS Y DASHBOARD ---
@app.post("/api/visitas/{mes_anio}")
async def registrar_visita(mes_anio: str, usuario: dict = Depends(obtener_usuario_actual)):
    try:
        existe = supabase.table('metas_mensuales').select('id, visitas_reales').eq('mes_anio', mes_anio).execute()
        if len(existe.data) > 0:
            visitas_actuales = existe.data[0].get('visitas_reales') or 0
            nueva_cantidad = visitas_actuales + 1
            supabase.table('metas_mensuales').update({"visitas_reales": nueva_cantidad}).eq('mes_anio', mes_anio).execute()
        else:
            nueva_cantidad = 1
            nueva_meta = {
                "mes_anio": mes_anio, "meta_ventas": 0, "meta_servicios": 0, "meta_clientes": 0, "meta_prospectos": 0, "meta_prospectos_visitas": 0, "visitas_reales": nueva_cantidad
            }
            supabase.table('metas_mensuales').insert(nueva_meta).execute()
        await manager.broadcast("update")
        return {"mensaje": "Visita registrada en BD", "visitas_reales": nueva_cantidad}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/dashboard/{mes_anio}")
async def obtener_dashboard(mes_anio: str, usuario: dict = Depends(requiere_rol("admin", "financiero"))):
    try:
        respuesta_metas = supabase.table('metas_mensuales').select('*').eq('mes_anio', mes_anio).execute()
        metas = respuesta_metas.data[0] if len(respuesta_metas.data) > 0 else { "meta_ventas": 0, "meta_servicios": 0, "meta_clientes": 0, "meta_prospectos": 0, "meta_prospectos_visitas": 0, "visitas_reales": 0 }
        mes, anio = mes_anio.split("-")
        filtro_fecha = f"{anio}-{mes}" 
        respuesta_servicios = supabase.table('servicios_aplicacion').select('costo_servicio', 'fecha_aplicacion', 'propiedad_id').execute()
        servicios_mes = [s for s in respuesta_servicios.data if s.get('fecha_aplicacion') and s.get('fecha_aplicacion', '').startswith(filtro_fecha)]
        ventas_reales = sum(float(s.get('costo_servicio') or 0) for s in servicios_mes)
        servicios_reales = len(servicios_mes)
        respuesta_clientes = supabase.table('clientes').select('id', 'created_at').execute()
        prospectos_mes = [c for c in respuesta_clientes.data if c.get('created_at') and c.get('created_at', '').startswith(filtro_fecha)]
        respuesta_propiedades = supabase.table('propiedades').select('id', 'cliente_id').execute()
        mapa_propiedades = {p['id']: p.get('cliente_id') for p in respuesta_propiedades.data if p.get('cliente_id')}
        clientes_activos_mes = set([mapa_propiedades[s.get('propiedad_id')] for s in servicios_mes if s.get('propiedad_id') and s.get('propiedad_id') in mapa_propiedades])
        return {
            "metas": metas, 
            "reales": {
                "ventas": ventas_reales, "servicios": servicios_reales, "clientes": len(clientes_activos_mes), "prospectos": len(prospectos_mes), "visitas": metas.get("visitas_reales", 0)
            }
        }
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/metas/")
async def guardar_metas(metas: MetasMensuales, usuario: dict = Depends(requiere_rol("admin", "financiero"))):
    try:
        existe = supabase.table('metas_mensuales').select('id').eq('mes_anio', metas.mes_anio).execute()
        if len(existe.data) > 0:
            supabase.table('metas_mensuales').update(metas.dict()).eq('mes_anio', metas.mes_anio).execute()
        else:
            supabase.table('metas_mensuales').insert(metas.dict()).execute()
        await manager.broadcast("update")
        return {"mensaje": "Metas guardadas correctamente"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))