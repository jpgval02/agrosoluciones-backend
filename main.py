from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from supabase import create_client, Client
try:
    from supabase.lib.client_options import ClientOptions
except Exception:
    ClientOptions = None
import os
import json
import logging
import traceback
import base64
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agrosoluciones")

# --- NUEVAS IMPORTACIONES PARA GOOGLE CALENDAR ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, date

load_dotenv()
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")
# Tiempos de espera más altos: si Supabase tarda un poco en responder (red lenta,
# arranque en frío, etc.), esperamos más antes de darlo por caído, en vez de
# cortar la conexión de golpe y perder la operación (guardar cliente, cotización, etc.)
try:
    if ClientOptions is None:
        raise RuntimeError("ClientOptions no disponible en esta versión de supabase-py")
    supabase: Client = create_client(url, key, options=ClientOptions(postgrest_client_timeout=30, storage_client_timeout=30))
except Exception:
    logger.error("No se pudo aplicar el timeout extendido a Supabase, usando conexión normal: %s", traceback.format_exc())
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
    dispositivo_id: Optional[str] = None

class Verifica2FA(BaseModel):
    factor_id: str
    codigo: str
    email: str
    dispositivo_id: Optional[str] = None

# --- SISTEMA DE PERMISOS POR ROL ---
def buscar_fila_de_rol(email: str):
    """
    Busca la fila de 'roles' para este correo comparando en Python (no con el
    filtro de Supabase), para blindarnos contra espacios invisibles u otras
    diferencias que el filtro ilike de la base de datos no perdona.

    Regresa: (fila_o_None, hubo_error_de_conexión)
    """
    objetivo = (email or "").strip().lower()
    try:
        todas = supabase.table('roles').select('rol, nombre, email').execute()
    except Exception:
        logger.error("No se pudo consultar la tabla 'roles' (probable corte de red con Supabase): %s", traceback.format_exc())
        return None, True
    for fila in (todas.data or []):
        if (fila.get('email') or "").strip().lower() == objetivo:
            return fila, False
    logger.error("Correo %r no encontró match en 'roles'. Correos existentes: %r", objetivo, [f.get('email') for f in (todas.data or [])])
    return None, False

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

    fila, hubo_error_conexion = buscar_fila_de_rol(email)
    if hubo_error_conexion:
        raise HTTPException(status_code=503, detail="No se pudo conectar con la base de datos en este momento. Intenta de nuevo en unos segundos.")

    rol = fila.get('rol') if fila else None
    nombre = fila.get('nombre') if fila else None

    if not rol:
        logger.error("Usuario autenticado sin rol asignado en la tabla 'roles': %s", email)
        raise HTTPException(status_code=403, detail="Tu cuenta no tiene un rol asignado. Contacta al administrador.")

    return {"email": email, "rol": rol, "nombre": nombre or email}


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
    cotizacion_id: Optional[str] = None
    descuento: Optional[float] = 0
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
    gasto_accesorios_refacciones: Optional[float] = 0.0
    agroquimico_usado: Optional[str] = ""
    ha_trabajadas: Optional[float] = 0.0
    precio_por_ha: Optional[float] = 0.0
    ingreso_viaticos: Optional[float] = 0.0
    ingreso_suministros: Optional[float] = 0.0
    metodo_pago: Optional[str] = "Efectivo"
    notificar_a: Optional[List[str]] = []

class ContactoNuevo(BaseModel):
    nombre: str
    email: str

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
    tipo_cultivo: Optional[str] = None
    hectareas: float
    precio_ha: float
    total: float
    estado: Optional[str] = "Pendiente"
    observaciones: Optional[str] = ""

class BitacoraMantenimientoNueva(BaseModel):
    equipo: str  # "Dron" o "Generador"
    tipo_mantenimiento: str
    fecha: str
    costo: Optional[float] = 0
    horas_uso: Optional[float] = None
    proximo_fecha: Optional[str] = None
    proximo_horas: Optional[float] = None
    notas: Optional[str] = ""

class HorometroActualizado(BaseModel):
    horas_actuales: float

class ChecklistParte(BaseModel):
    items: List[dict]  # [{"item": "Dron", "revisado": true}, ...]

class ReporteServicioInfo(BaseModel):
    hectareas_trabajadas: Optional[float] = None
    incidencias: Optional[str] = ""
    clima: Optional[str] = ""
    observaciones: Optional[str] = ""

class FirmaBase64(BaseModel):
    imagen_base64: str  # data:image/png;base64,....

class FotoAEliminar(BaseModel):
    url: str

class AcompananteNuevo(BaseModel):
    contacto_id: int

class ObservacionNueva(BaseModel):
    texto: str


# --- FUNCIÓN DE GOOGLE CALENDAR (ACTUALIZADA PARA INVITADOS) ---
def agendar_en_google_calendar(fecha, no_cotizacion, observaciones, nombre_productor, parcela, hectareas, notificar_a=None, event_id=None):
    """
    Crea o actualiza el evento de la cita en Google Calendar.
    - Si event_id es None: crea un evento nuevo y regresa su ID.
    - Si event_id ya existe: actualiza los invitados de ESE mismo evento
      (para cuando en Operaciones se agrega o quita gente de la asignación,
      en vez de crear un evento duplicado cada vez).
    Regresa el ID del evento (nuevo o el mismo que se actualizó), o None si no se pudo agendar.
    """
    CORREO_CALENDARIO = 'facturacion@asoa.com.mx'
    ARCHIVO_CREDENCIALES = 'credenciales_calendario.json'

    correos_equipo = [{'email': e} for e in (notificar_a or []) if e]

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
        servicio_cal = build('calendar', 'v3', credentials=creds)

        evento = {
            'summary': f'🚜 Vuelo: {nombre_productor} ({hectareas} Ha.)',
            'location': parcela,
            'description': f'Cotización/OS: {no_cotizacion}\nHectáreas: {hectareas} Ha.\nObservaciones: {observaciones}',
            'start': {'date': fecha, 'timeZone': 'America/Mexico_City'},
            'end': {'date': fecha, 'timeZone': 'America/Mexico_City'},
        }

        # --- Ya existe un evento para este servicio: lo actualizamos (no creamos uno nuevo) ---
        if event_id:
            try:
                evento['attendees'] = correos_equipo
                resultado = servicio_cal.events().patch(
                    calendarId=CORREO_CALENDARIO, eventId=event_id, body=evento, sendUpdates='all'
                ).execute()
                print("¡Evento actualizado con la nueva lista de invitados!")
                return resultado.get('id', event_id)
            except Exception as update_err:
                if 'forbiddenForServiceAccounts' in str(update_err) or 'Domain-Wide Delegation' in str(update_err):
                    evento.pop('attendees', None)
                    resultado = servicio_cal.events().patch(calendarId=CORREO_CALENDARIO, eventId=event_id, body=evento).execute()
                    return resultado.get('id', event_id)
                if 'notFound' in str(update_err) or '404' in str(update_err):
                    # El evento ya no existe (se borró a mano en Calendar) -> lo creamos de nuevo abajo
                    event_id = None
                else:
                    raise

        # --- No hay evento todavía: lo creamos ---
        if not correos_equipo:
            resultado = servicio_cal.events().insert(calendarId=CORREO_CALENDARIO, body=evento).execute()
            print("¡Cita agendada sin invitados (no se seleccionó a nadie)!")
            return resultado.get('id')

        try:
            evento['attendees'] = correos_equipo
            resultado = servicio_cal.events().insert(
                calendarId=CORREO_CALENDARIO,
                body=evento,
                sendUpdates='all'  # <-- ESTO LES AVISA POR CORREO
            ).execute()
            print("¡Cita agendada y equipo notificado con éxito!")
            return resultado.get('id')
        except Exception as invite_err:
            if 'forbiddenForServiceAccounts' in str(invite_err) or 'Domain-Wide Delegation' in str(invite_err):
                print("No se pudo invitar al equipo (falta Domain-Wide Delegation). Se crea la cita sin invitados.")
                evento.pop('attendees', None)
                resultado = servicio_cal.events().insert(calendarId=CORREO_CALENDARIO, body=evento).execute()
                print("¡Cita agendada sin invitados!")
                return resultado.get('id')
            else:
                raise

    except Exception as e:
        print(f"Error con Google Calendar: {e}")
        return None


# --- RUTA DE LOGIN PRINCIPAL ---
@app.post("/login/")
async def iniciar_sesion(credenciales: Credenciales):
    try:
        respuesta = supabase.auth.sign_in_with_password({
            "email": credenciales.email,
            "password": credenciales.password
        })

        # --- ¿Este dispositivo ya quedó confiado hoy? Si sí, saltamos el 2FA ---
        if credenciales.dispositivo_id:
            hoy = date.today().isoformat()
            confiable = supabase.table('dispositivos_confiables').select("id").eq('email', credenciales.email).eq('dispositivo_id', credenciales.dispositivo_id).eq('fecha', hoy).execute()
            if confiable.data:
                fila, _ = buscar_fila_de_rol(credenciales.email)
                rol_usuario = fila.get('rol') if fila else None

                # Si por cualquier motivo no se pudo determinar el rol real, NO adivinamos
                # (antes esto se quedaba en "operador" por default, causando que un admin
                # apareciera como operador). Mejor caemos al flujo normal de 2FA.
                if rol_usuario:
                    sesion = getattr(respuesta, 'session', None)
                    access_token = getattr(sesion, 'access_token', None) if sesion else None
                    return {"mensaje": "Acceso concedido", "necesita_2fa": False, "rol": rol_usuario, "access_token": access_token}

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
        
        fila, hubo_error_conexion = buscar_fila_de_rol(req.email)
        rol_usuario = fila.get('rol') if fila else None

        if hubo_error_conexion:
            raise HTTPException(status_code=503, detail="No se pudo conectar con la base de datos en este momento. Intenta de nuevo en unos segundos.")
        if not rol_usuario:
            raise HTTPException(status_code=403, detail="Tu cuenta no tiene un rol asignado. Contacta al administrador.")

        access_token = getattr(verificacion, 'access_token', None) if hasattr(verificacion, 'access_token') else verificacion.get('access_token')

        if req.dispositivo_id:
            try:
                hoy = date.today().isoformat()
                supabase.table('dispositivos_confiables').upsert({
                    "email": req.email, "dispositivo_id": req.dispositivo_id, "fecha": hoy
                }, on_conflict="email,dispositivo_id,fecha").execute()
            except Exception:
                pass

        return {"mensaje": "Acceso concedido", "rol": rol_usuario, "access_token": access_token}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail="Código 2FA incorrecto o expirado.")

# --- RUTAS DE CLIENTES ---
@app.post("/clientes/")
async def registrar_cliente(cliente: ClienteNuevo, usuario: dict = Depends(requiere_rol("admin", "operador", "auxiliar_del_auxiliar"))):
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
async def actualizar_cliente(cliente_id: str, cliente: ClienteNuevo, usuario: dict = Depends(requiere_rol("admin", "operador", "auxiliar_del_auxiliar"))):
    try:
        respuesta = supabase.table('clientes').update(cliente.dict()).eq('id', cliente_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/clientes/{cliente_id}")
async def eliminar_cliente(cliente_id: str, usuario: dict = Depends(requiere_rol("admin", "auxiliar_del_auxiliar"))):
    try:
        supabase.table('clientes').delete().eq('id', cliente_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Eliminado exitosamente"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- RUTAS DE PROPIEDADES ---
@app.post("/propiedades/")
async def registrar_propiedad(propiedad: PropiedadNueva, usuario: dict = Depends(requiere_rol("admin", "operador", "auxiliar_del_auxiliar"))):
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
async def actualizar_propiedad(propiedad_id: str, propiedad: PropiedadNueva, usuario: dict = Depends(requiere_rol("admin", "operador", "auxiliar_del_auxiliar"))):
    try:
        respuesta = supabase.table('propiedades').update(propiedad.dict()).eq('id', propiedad_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/propiedades/{propiedad_id}")
async def eliminar_propiedad(propiedad_id: str, usuario: dict = Depends(requiere_rol("admin", "auxiliar_del_auxiliar"))):
    try:
        supabase.table('propiedades').delete().eq('id', propiedad_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Eliminado exitosamente"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- RUTAS DE SERVICIOS (MODIFICADA CON GOOGLE CALENDAR) ---
CAMPOS_FINANCIEROS_SERVICIO = [
    'costo_servicio', 'gastos', 'gasto_gasolina_unidad', 'gasto_gasolina_generador',
    'gasto_sueldos', 'gasto_insumos', 'gasto_comidas', 'gasto_oxxo', 'gasto_accesorios_refacciones',
    'precio_por_ha', 'ingreso_viaticos', 'ingreso_suministros', 'descuento'
]

@app.post("/servicios/")
async def registrar_servicio(servicio: ServicioNuevo, usuario: dict = Depends(requiere_rol("admin", "operador", "auxiliar_del_auxiliar"))):
    try:
        respuesta = supabase.table('servicios_aplicacion').insert(servicio.dict(exclude={'notificar_a'})).execute()

        # Si esta venta viene de una cotización, la marcamos como convertida (relación real, no por texto)
        if servicio.cotizacion_id:
            try:
                supabase.table('cotizaciones').update({'estado': 'Convertida'}).eq('id', servicio.cotizacion_id).execute()
            except Exception as cot_err:
                print(f"No se pudo marcar la cotización como convertida: {cot_err}")
        
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

        # El agendado a Google Calendar y la selección de a quién avisar ya NO se hacen aquí:
        # ahora se hacen en Operaciones, dentro de "Asignación", cuando el jefe de
        # operaciones o el admin asignen el servicio.

        return {"mensaje": "Guardado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/servicios/")
async def obtener_servicios(usuario: dict = Depends(obtener_usuario_actual)):
    respuesta = supabase.table('servicios_aplicacion').select("*").execute()
    datos = respuesta.data
    if usuario["rol"] in ("operador", "auxiliar_del_auxiliar"):
        for fila in datos:
            for campo in CAMPOS_FINANCIEROS_SERVICIO:
                fila.pop(campo, None)
    return datos

# --- DIRECTORIO DE CONTACTOS PARA NOTIFICAR EN EL CALENDARIO (no requiere cuenta en el sistema) ---
@app.get("/contactos-notificacion/")
async def obtener_contactos(usuario: dict = Depends(obtener_usuario_actual)):
    respuesta = supabase.table('contactos_notificacion').select("*").order('nombre').execute()
    return respuesta.data

@app.post("/contactos-notificacion/")
async def crear_contacto(contacto: ContactoNuevo, usuario: dict = Depends(requiere_rol("admin"))):
    try:
        respuesta = supabase.table('contactos_notificacion').insert(contacto.dict()).execute()
        return {"mensaje": "Contacto agregado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/contactos-notificacion/{contacto_id}")
async def eliminar_contacto(contacto_id: str, usuario: dict = Depends(requiere_rol("admin"))):
    try:
        supabase.table('contactos_notificacion').delete().eq('id', contacto_id).execute()
        return {"mensaje": "Contacto eliminado"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.put("/servicios/{servicio_id}")
async def actualizar_servicio(servicio_id: str, servicio: ServicioNuevo, usuario: dict = Depends(requiere_rol("admin", "operador", "auxiliar_del_auxiliar"))):
    try:
        # Vemos si esta venta ya tenía otra cotización ligada, para poder liberarla si cambió
        anterior = supabase.table('servicios_aplicacion').select('cotizacion_id').eq('id', servicio_id).execute()
        cotizacion_id_anterior = anterior.data[0].get('cotizacion_id') if len(anterior.data) > 0 else None

        respuesta = supabase.table('servicios_aplicacion').update(servicio.dict(exclude={'notificar_a'})).eq('id', servicio_id).execute()

        if cotizacion_id_anterior and cotizacion_id_anterior != servicio.cotizacion_id:
            try: supabase.table('cotizaciones').update({'estado': 'Pendiente'}).eq('id', cotizacion_id_anterior).execute()
            except Exception as cot_err: print(f"No se pudo liberar la cotización anterior: {cot_err}")

        if servicio.cotizacion_id and servicio.cotizacion_id != cotizacion_id_anterior:
            try: supabase.table('cotizaciones').update({'estado': 'Convertida'}).eq('id', servicio.cotizacion_id).execute()
            except Exception as cot_err: print(f"No se pudo marcar la cotización como convertida: {cot_err}")

        await manager.broadcast("update")
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/servicios/{servicio_id}")
async def eliminar_servicio(servicio_id: str, usuario: dict = Depends(requiere_rol("admin", "auxiliar_del_auxiliar"))):
    try:
        # Si esta venta tenía una cotización ligada, la regresamos a "Pendiente" al borrarla
        existente = supabase.table('servicios_aplicacion').select('cotizacion_id').eq('id', servicio_id).execute()
        if len(existente.data) > 0 and existente.data[0].get('cotizacion_id'):
            try: supabase.table('cotizaciones').update({'estado': 'Pendiente'}).eq('id', existente.data[0]['cotizacion_id']).execute()
            except Exception as cot_err: print(f"No se pudo liberar la cotización: {cot_err}")

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
def _numero_de_cotizacion(observaciones) -> str:
    """El número de cotización vive dentro del JSON de 'observaciones' (campo no_cotizacion)."""
    try:
        return str((json.loads(observaciones) if observaciones else {}).get("no_cotizacion") or "").strip()
    except Exception:
        return ""

def _verificar_numero_cotizacion_libre(numero: str, excluir_id=None):
    """Evita que dos cotizaciones tengan el mismo número (por ejemplo, si dos personas capturan a la vez)."""
    if not numero:
        return
    filas = supabase.table('cotizaciones').select("id, observaciones").execute().data or []
    for f in filas:
        if excluir_id is not None and str(f.get("id")) == str(excluir_id):
            continue
        if _numero_de_cotizacion(f.get("observaciones")) == numero:
            raise HTTPException(status_code=409, detail=f"Ya existe una cotización con el número {numero}.")

@app.post("/cotizaciones/")
async def registrar_cotizacion(cot: CotizacionNueva, usuario: dict = Depends(requiere_rol("admin", "operador", "auxiliar_del_auxiliar"))):
    try:
        datos = cot.dict()
        _verificar_numero_cotizacion_libre(_numero_de_cotizacion(datos.get("observaciones")))
        datos["creado_por_email"] = usuario["email"]
        datos["creado_por_nombre"] = usuario["nombre"]

        # Si quien la crea ya es el jefe (Admin), se autoriza sola. Si no, queda pendiente de que el jefe la revise.
        if usuario["rol"] == "admin":
            datos["estado_autorizacion"] = "aprobada"
            datos["aprobado_por_email"] = usuario["email"]
            datos["aprobado_por_nombre"] = usuario["nombre"]
        else:
            datos["estado_autorizacion"] = "pendiente"

        respuesta = supabase.table('cotizaciones').insert(datos).execute()

        # Semáforo de clientes: si estaba en "Prospecto", lo subimos a "Negociación".
        # Si ya era "Cliente" (venta real), lo dejamos así, no lo bajamos de categoría.
        try:
            cliente_actual = supabase.table('clientes').select('estado').eq('id', cot.cliente_id).execute()
            if len(cliente_actual.data) > 0 and cliente_actual.data[0].get('estado') != 'Cliente':
                supabase.table('clientes').update({'estado': 'Negociación'}).eq('id', cot.cliente_id).execute()
        except Exception as est_err:
            print(f"No se pudo actualizar el semáforo del cliente: {est_err}")

        await manager.broadcast("update")
        return {"mensaje": "Cotización guardada", "datos": respuesta.data[0]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/cotizaciones/")
async def obtener_cotizaciones(usuario: dict = Depends(obtener_usuario_actual)):
    respuesta = supabase.table('cotizaciones').select("*").execute()
    return respuesta.data

@app.put("/cotizaciones/{cot_id}")
async def actualizar_cotizacion(cot_id: str, cot: CotizacionNueva, usuario: dict = Depends(requiere_rol("admin", "operador", "auxiliar_del_auxiliar"))):
    try:
        datos = cot.dict()
        # Solo se revisa duplicado si el número cambió (para no bloquear ediciones de cotizaciones viejas)
        anterior = supabase.table('cotizaciones').select("observaciones").eq('id', cot_id).execute().data
        numero_nuevo = _numero_de_cotizacion(datos.get("observaciones"))
        if not anterior or _numero_de_cotizacion(anterior[0].get("observaciones")) != numero_nuevo:
            _verificar_numero_cotizacion_libre(numero_nuevo, excluir_id=cot_id)
        # Si quien edita no es Admin, cualquier corrección regresa la cotización a "pendiente" de revisión
        if usuario["rol"] != "admin":
            datos["estado_autorizacion"] = "pendiente"
            datos["comentario_rechazo"] = None
        respuesta = supabase.table('cotizaciones').update(datos).eq('id', cot_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

class RechazoCotizacion(BaseModel):
    comentario: str

@app.put("/cotizaciones/{cot_id}/autorizar")
async def autorizar_cotizacion(cot_id: str, usuario: dict = Depends(requiere_rol("admin"))):
    try:
        supabase.table('cotizaciones').update({
            'estado_autorizacion': 'aprobada',
            'aprobado_por_email': usuario["email"],
            'aprobado_por_nombre': usuario["nombre"],
            'comentario_rechazo': None
        }).eq('id', cot_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Cotización autorizada"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.put("/cotizaciones/{cot_id}/rechazar")
async def rechazar_cotizacion(cot_id: str, cuerpo: RechazoCotizacion, usuario: dict = Depends(requiere_rol("admin"))):
    try:
        supabase.table('cotizaciones').update({
            'estado_autorizacion': 'rechazada',
            'comentario_rechazo': cuerpo.comentario
        }).eq('id', cot_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Cotización rechazada"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- FIRMA DIGITAL DEL JEFE (Admin) ---
@app.get("/firmas/mia")
async def obtener_mi_firma(usuario: dict = Depends(requiere_rol("admin", "jefe_operaciones", "operador", "auxiliar_del_auxiliar"))):
    respuesta = supabase.table('firmas').select("*").eq('email', usuario["email"]).execute()
    return respuesta.data[0] if respuesta.data else None

@app.get("/firmas/{email}")
async def obtener_firma_de(email: str, usuario: dict = Depends(obtener_usuario_actual)):
    respuesta = supabase.table('firmas').select("*").eq('email', email).execute()
    return respuesta.data[0] if respuesta.data else None

@app.post("/firmas/")
async def subir_firma(archivo: UploadFile = File(...), usuario: dict = Depends(requiere_rol("admin", "jefe_operaciones", "operador", "auxiliar_del_auxiliar"))):
    contenido = await archivo.read()
    extension = archivo.filename.split('.')[-1] if '.' in archivo.filename else 'png'
    nombre_archivo = f"{usuario['email']}.{extension}"

    # Paso 1: subir el archivo al bucket de Storage
    try:
        supabase.storage.from_('firmas').upload(
            nombre_archivo, contenido,
            {"content-type": archivo.content_type or "image/png", "x-upsert": "true"}
        )
    except Exception as e:
        logger.error("Fallo al subir a Storage bucket 'firmas': %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error al subir a Storage: {e}")

    # Paso 2: obtener la URL pública
    try:
        url_publica = supabase.storage.from_('firmas').get_public_url(nombre_archivo)
    except Exception as e:
        logger.error("Fallo al obtener URL pública: %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error al obtener URL pública: {e}")

    # Paso 3: guardar/actualizar el registro en la tabla 'firmas'
    try:
        supabase.table('firmas').upsert(
            {'email': usuario["email"], 'url_firma': url_publica},
            on_conflict="email"
        ).execute()
    except Exception as e:
        logger.error("Fallo al guardar en tabla 'firmas': %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error al guardar en la tabla: {e}")

    return {"mensaje": "Firma guardada", "url": url_publica}

@app.delete("/cotizaciones/{cot_id}")
async def eliminar_cotizacion(cot_id: str, usuario: dict = Depends(requiere_rol("admin", "auxiliar_del_auxiliar"))):
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
        respuesta_servicios = supabase.table('servicios_aplicacion').select('costo_servicio', 'fecha_aplicacion', 'propiedad_id', 'liberado', 'cuenta_legacy').execute()
        servicios_mes_todos = [s for s in respuesta_servicios.data if s.get('fecha_aplicacion') and s.get('fecha_aplicacion', '').startswith(filtro_fecha)]
        # El dinero solo cuenta si el servicio es "legacy" (ya existía antes de este cambio)
        # o si Operaciones ya lo liberó (checklist validado por el jefe de operaciones).
        servicios_mes = [s for s in servicios_mes_todos if s.get('cuenta_legacy') or s.get('liberado')]
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


# ============================================================
# --- OPERACIONES: BITÁCORAS DE MANTENIMIENTO (Dron / Generador) ---
# Registran: admin y jefe_operaciones. Ven: cualquier usuario logueado.
# ============================================================
@app.get("/operaciones/bitacoras/")
async def obtener_bitacoras(usuario: dict = Depends(obtener_usuario_actual)):
    respuesta = supabase.table('bitacoras_mantenimiento').select("*").order('fecha', desc=True).execute()
    return respuesta.data

@app.post("/operaciones/bitacoras/")
async def registrar_bitacora(bit: BitacoraMantenimientoNueva, usuario: dict = Depends(requiere_rol("admin", "jefe_operaciones"))):
    try:
        if bit.equipo not in ("Dron", "Generador"):
            raise HTTPException(status_code=400, detail="Equipo inválido.")
        datos = bit.dict()
        datos["realizado_por_email"] = usuario["email"]
        datos["realizado_por_nombre"] = usuario["nombre"]
        respuesta = supabase.table('bitacoras_mantenimiento').insert(datos).execute()
        await manager.broadcast("update")
        return {"mensaje": "Bitácora guardada", "datos": respuesta.data[0]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.put("/operaciones/bitacoras/{bitacora_id}")
async def actualizar_bitacora(bitacora_id: str, bit: BitacoraMantenimientoNueva, usuario: dict = Depends(requiere_rol("admin", "jefe_operaciones"))):
    try:
        respuesta = supabase.table('bitacoras_mantenimiento').update(bit.dict()).eq('id', bitacora_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Actualizada", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/operaciones/bitacoras/{bitacora_id}")
async def eliminar_bitacora(bitacora_id: str, usuario: dict = Depends(requiere_rol("admin", "jefe_operaciones"))):
    try:
        supabase.table('bitacoras_mantenimiento').delete().eq('id', bitacora_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Eliminada"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- Horómetro actual de cada equipo (para la alerta por horas de uso) ---
@app.get("/operaciones/equipos-estado/")
async def obtener_equipos_estado(usuario: dict = Depends(obtener_usuario_actual)):
    respuesta = supabase.table('equipos_estado').select("*").execute()
    return respuesta.data

@app.put("/operaciones/equipos-estado/{equipo}")
async def actualizar_horometro(equipo: str, cuerpo: HorometroActualizado, usuario: dict = Depends(requiere_rol("admin", "jefe_operaciones"))):
    try:
        if equipo not in ("Dron", "Generador"):
            raise HTTPException(status_code=400, detail="Equipo inválido.")
        supabase.table('equipos_estado').upsert({
            "equipo": equipo,
            "horas_actuales": cuerpo.horas_actuales,
            "actualizado_por_email": usuario["email"],
            "actualizado_en": datetime.utcnow().isoformat()
        }, on_conflict="equipo").execute()
        await manager.broadcast("update")
        return {"mensaje": "Horómetro actualizado"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# --- OPERACIONES: INVENTARIO / CHECKLIST (Sale y Regresa) ---
# Lo marca: operador. La validación/liberación ya NO se hace aquí: se hace
# desde el Reporte de Servicio con el botón "Liberar y descargar PDF".
# ============================================================
@app.get("/operaciones/checklist/{servicio_id}")
async def obtener_checklist(servicio_id: str, usuario: dict = Depends(obtener_usuario_actual)):
    respuesta = supabase.table('checklists_servicio').select("*").eq('servicio_id', servicio_id).execute()
    return respuesta.data[0] if respuesta.data else None

@app.post("/operaciones/checklist/{servicio_id}/{tipo}")
async def guardar_checklist(servicio_id: str, tipo: str, cuerpo: ChecklistParte, usuario: dict = Depends(requiere_rol("admin", "operador", "jefe_operaciones"))):
    try:
        if tipo not in ("salida", "regreso"):
            raise HTTPException(status_code=400, detail="Tipo inválido, debe ser 'salida' o 'regreso'.")

        existente = supabase.table('checklists_servicio').select("id, validado").eq('servicio_id', servicio_id).execute()
        if existente.data and existente.data[0].get("validado"):
            raise HTTPException(status_code=400, detail="Este checklist ya fue validado y liberado, no se puede modificar.")

        ahora = datetime.utcnow().isoformat()
        datos = {
            f"items_{tipo}": cuerpo.items,
            f"fecha_{tipo}": ahora,
            f"operador_{tipo}_email": usuario["email"],
            f"operador_{tipo}_nombre": usuario["nombre"],
        }
        if existente.data:
            supabase.table('checklists_servicio').update(datos).eq('servicio_id', servicio_id).execute()
        else:
            datos["servicio_id"] = servicio_id
            supabase.table('checklists_servicio').insert(datos).execute()

        await manager.broadcast("update")
        return {"mensaje": f"Checklist de {tipo} guardado"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# --- OPERACIONES: REPORTE DE SERVICIO ---
# Lo llenan: admin, operador, jefe_operaciones. Se bloquea si el
# servicio ya fue liberado (checklist validado).
# ============================================================
def _verificar_servicio_no_liberado(servicio_id: str):
    res = supabase.table('servicios_aplicacion').select('liberado').eq('id', servicio_id).execute()
    if res.data and res.data[0].get('liberado'):
        raise HTTPException(status_code=400, detail="Este servicio ya fue liberado, el reporte no se puede modificar.")

@app.get("/operaciones/reporte/{servicio_id}")
async def obtener_reporte_servicio(servicio_id: str, usuario: dict = Depends(obtener_usuario_actual)):
    respuesta = supabase.table('reportes_servicio').select("*").eq('servicio_id', servicio_id).execute()
    if not respuesta.data:
        return None
    reporte = dict(respuesta.data[0])
    # Datos de la liberación (para el PDF final): quién liberó, cuándo y su firma
    reporte["liberado"] = False
    try:
        srv = supabase.table('servicios_aplicacion').select("liberado, liberado_por_email, liberado_por_nombre, fecha_liberacion").eq('id', servicio_id).execute()
        if srv.data and srv.data[0].get("liberado"):
            fila = srv.data[0]
            reporte["liberado"] = True
            reporte["liberado_por_nombre"] = fila.get("liberado_por_nombre")
            reporte["fecha_liberacion"] = fila.get("fecha_liberacion")
            if fila.get("liberado_por_email"):
                f = supabase.table('firmas').select("url_firma").eq('email', fila["liberado_por_email"]).execute()
                reporte["firma_liberador_url"] = f.data[0]["url_firma"] if f.data else None
    except Exception:
        logger.error("No se pudieron leer los datos de liberación: %s", traceback.format_exc())
    return reporte

# Lista de servicios que ya tienen un reporte guardado (para la columna "Reporte" de Ventas BD)
@app.get("/operaciones/reportes-existentes")
async def reportes_existentes(usuario: dict = Depends(obtener_usuario_actual)):
    try:
        respuesta = supabase.table('reportes_servicio').select("servicio_id").execute()
        return [fila["servicio_id"] for fila in (respuesta.data or [])]
    except Exception:
        logger.error("No se pudo leer la lista de reportes: %s", traceback.format_exc())
        return []

# ÚNICO punto de liberación: el botón "Liberar y descargar PDF" (solo admin y jefe_operaciones).
# Exige checklist de salida y regreso, reporte con hectáreas, ambas firmas y la firma del jefe.
@app.post("/operaciones/reporte/{servicio_id}/liberar")
async def liberar_servicio(servicio_id: str, usuario: dict = Depends(requiere_rol("admin", "jefe_operaciones"))):
    try:
        srv = supabase.table('servicios_aplicacion').select("id, liberado").eq('id', servicio_id).execute()
        if not srv.data:
            raise HTTPException(status_code=404, detail="Servicio no encontrado.")
        if srv.data[0].get("liberado"):
            raise HTTPException(status_code=400, detail="Este servicio ya fue liberado.")

        faltan = []
        chk = supabase.table('checklists_servicio').select("*").eq('servicio_id', servicio_id).execute()
        fila_chk = chk.data[0] if chk.data else {}
        if not fila_chk.get("items_salida"): faltan.append("checklist de salida")
        if not fila_chk.get("items_regreso"): faltan.append("checklist de regreso")

        rep = supabase.table('reportes_servicio').select("*").eq('servicio_id', servicio_id).execute()
        fila_rep = rep.data[0] if rep.data else {}
        if fila_rep.get("hectareas_trabajadas") is None: faltan.append("hectáreas trabajadas en el reporte")
        if not fila_rep.get("firma_cliente_url"): faltan.append("firma del cliente")
        if not fila_rep.get("firma_operador_url"): faltan.append("firma del operador")

        firma = supabase.table('firmas').select("url_firma").eq('email', usuario["email"]).execute()
        if not firma.data: faltan.append("tu firma en 'Mi Firma'")

        if faltan:
            raise HTTPException(status_code=400, detail="Para liberar falta: " + ", ".join(faltan) + ".")

        ahora = datetime.utcnow().isoformat()
        supabase.table('checklists_servicio').update({
            "validado": True,
            "validado_por_email": usuario["email"],
            "validado_por_nombre": usuario["nombre"],
            "fecha_validacion": ahora
        }).eq('servicio_id', servicio_id).execute()

        supabase.table('servicios_aplicacion').update({
            "liberado": True,
            "liberado_por_email": usuario["email"],
            "liberado_por_nombre": usuario["nombre"],
            "fecha_liberacion": ahora
        }).eq('id', servicio_id).execute()

        await manager.broadcast("update")
        return {"mensaje": "Servicio liberado"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/operaciones/reporte/{servicio_id}")
async def guardar_reporte_servicio(servicio_id: str, info: ReporteServicioInfo, usuario: dict = Depends(requiere_rol("admin", "operador", "jefe_operaciones"))):
    try:
        _verificar_servicio_no_liberado(servicio_id)
        existente = supabase.table('reportes_servicio').select("id").eq('servicio_id', servicio_id).execute()
        datos = info.dict()
        datos["actualizado_en"] = datetime.utcnow().isoformat()
        if existente.data:
            supabase.table('reportes_servicio').update(datos).eq('servicio_id', servicio_id).execute()
        else:
            datos["servicio_id"] = servicio_id
            datos["creado_por_email"] = usuario["email"]
            datos["creado_por_nombre"] = usuario["nombre"]
            supabase.table('reportes_servicio').insert(datos).execute()
        await manager.broadcast("update")
        return {"mensaje": "Reporte guardado"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

def _obtener_o_crear_reporte(servicio_id: str, usuario: dict):
    existente = supabase.table('reportes_servicio').select("*").eq('servicio_id', servicio_id).execute()
    if existente.data:
        return existente.data[0]
    nuevo = {"servicio_id": servicio_id, "creado_por_email": usuario["email"], "creado_por_nombre": usuario["nombre"], "fotos": []}
    creado = supabase.table('reportes_servicio').insert(nuevo).execute()
    return creado.data[0]

@app.post("/operaciones/reporte/{servicio_id}/foto")
async def subir_foto_reporte(servicio_id: str, archivo: UploadFile = File(...), usuario: dict = Depends(requiere_rol("admin", "operador", "jefe_operaciones"))):
    try:
        _verificar_servicio_no_liberado(servicio_id)
        reporte = _obtener_o_crear_reporte(servicio_id, usuario)

        contenido = await archivo.read()
        extension = archivo.filename.split('.')[-1] if '.' in archivo.filename else 'jpg'
        nombre_archivo = f"{servicio_id}/foto_{int(datetime.utcnow().timestamp()*1000)}.{extension}"

        supabase.storage.from_('reportes_servicio').upload(
            nombre_archivo, contenido, {"content-type": archivo.content_type, "upsert": "true"}
        )
        url_publica = supabase.storage.from_('reportes_servicio').get_public_url(nombre_archivo)

        fotos = reporte.get("fotos") or []
        fotos.append(url_publica)
        supabase.table('reportes_servicio').update({"fotos": fotos, "actualizado_en": datetime.utcnow().isoformat()}).eq('servicio_id', servicio_id).execute()

        await manager.broadcast("update")
        return {"mensaje": "Foto agregada", "url": url_publica}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/operaciones/reporte/{servicio_id}/foto")
async def eliminar_foto_reporte(servicio_id: str, cuerpo: FotoAEliminar, usuario: dict = Depends(requiere_rol("admin", "operador", "jefe_operaciones"))):
    try:
        _verificar_servicio_no_liberado(servicio_id)
        existente = supabase.table('reportes_servicio').select("fotos").eq('servicio_id', servicio_id).execute()
        if not existente.data:
            raise HTTPException(status_code=404, detail="No hay reporte para este servicio.")
        fotos = [f for f in (existente.data[0].get("fotos") or []) if f != cuerpo.url]
        supabase.table('reportes_servicio').update({"fotos": fotos, "actualizado_en": datetime.utcnow().isoformat()}).eq('servicio_id', servicio_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Foto eliminada"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/operaciones/reporte/{servicio_id}/firma/{quien}")
async def guardar_firma_reporte(servicio_id: str, quien: str, cuerpo: FirmaBase64, usuario: dict = Depends(requiere_rol("admin", "operador", "jefe_operaciones"))):
    try:
        if quien not in ("cliente", "operador"):
            raise HTTPException(status_code=400, detail="quien debe ser 'cliente' u 'operador'.")
        _verificar_servicio_no_liberado(servicio_id)
        _obtener_o_crear_reporte(servicio_id, usuario)

        b64 = cuerpo.imagen_base64.split(",")[-1]
        contenido = base64.b64decode(b64)
        nombre_archivo = f"{servicio_id}/firma_{quien}.png"

        supabase.storage.from_('reportes_servicio').upload(
            nombre_archivo, contenido, {"content-type": "image/png", "upsert": "true"}
        )
        url_publica = supabase.storage.from_('reportes_servicio').get_public_url(nombre_archivo)

        supabase.table('reportes_servicio').update({
            f"firma_{quien}_url": url_publica,
            "actualizado_en": datetime.utcnow().isoformat()
        }).eq('servicio_id', servicio_id).execute()

        await manager.broadcast("update")
        return {"mensaje": "Firma guardada", "url": url_publica}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# --- OPERACIONES: ASIGNACIÓN (antes "Acompañantes") ---
# Aquí es donde el jefe_operaciones o admin elige a quién se le avisa de
# cada servicio. Se eligen del Directorio de Notificaciones ya existente.
# Cada cambio (agregar o quitar) sincroniza el MISMO evento de Google
# Calendar (lo crea la primera vez, después solo actualiza invitados).
# ============================================================
def _sincronizar_calendario_servicio(servicio_id: str):
    try:
        serv = supabase.table('servicios_aplicacion').select("*").eq('id', servicio_id).execute()
        if not serv.data:
            return
        s = serv.data[0]
        if not s.get('fecha_aplicacion'):
            return

        nombre_productor = "Productor Desconocido"
        nombre_parcela = "Ubicación Desconocida"
        try:
            prop_data = supabase.table('propiedades').select('cliente_id, nombre_propiedad, direccion').eq('id', s.get('propiedad_id')).execute()
            if prop_data.data:
                nombre_parcela = f"{prop_data.data[0]['nombre_propiedad']} ({prop_data.data[0]['direccion']})"
                cliente_data = supabase.table('clientes').select('nombre, apellidos').eq('id', prop_data.data[0]['cliente_id']).execute()
                if cliente_data.data:
                    nombre_productor = f"{cliente_data.data[0]['nombre']} {cliente_data.data[0]['apellidos']}"
        except Exception:
            pass

        asignados = supabase.table('servicio_acompanantes').select("contacto_id").eq('servicio_id', servicio_id).execute()
        emails = []
        for a in (asignados.data or []):
            c = supabase.table('contactos_notificacion').select('email').eq('id', a['contacto_id']).execute()
            if c.data and c.data[0].get('email'):
                emails.append(c.data[0]['email'])

        nuevo_event_id = agendar_en_google_calendar(
            fecha=s.get('fecha_aplicacion'),
            no_cotizacion=s.get('no_cotizacion') or 'S/N',
            observaciones=s.get('observaciones') or 'Sin detalles',
            nombre_productor=nombre_productor,
            parcela=nombre_parcela,
            hectareas=s.get('ha_trabajadas') or 0,
            notificar_a=emails,
            event_id=s.get('google_event_id')
        )
        if nuevo_event_id and nuevo_event_id != s.get('google_event_id'):
            supabase.table('servicios_aplicacion').update({'google_event_id': nuevo_event_id}).eq('id', servicio_id).execute()
    except Exception:
        logger.error("No se pudo sincronizar el calendario para el servicio %s: %s", servicio_id, traceback.format_exc())

@app.get("/operaciones/acompanantes/{servicio_id}")
async def obtener_acompanantes(servicio_id: str, usuario: dict = Depends(obtener_usuario_actual)):
    respuesta = supabase.table('servicio_acompanantes').select("*").eq('servicio_id', servicio_id).order('agregado_en').execute()
    return respuesta.data

@app.post("/operaciones/acompanantes/{servicio_id}")
async def agregar_acompanante(servicio_id: str, cuerpo: AcompananteNuevo, usuario: dict = Depends(requiere_rol("admin", "jefe_operaciones"))):
    try:
        _verificar_servicio_no_liberado(servicio_id)
        contacto = supabase.table('contactos_notificacion').select("*").eq('id', cuerpo.contacto_id).execute()
        if not contacto.data:
            raise HTTPException(status_code=404, detail="Contacto no encontrado en el Directorio de Notificaciones.")
        datos = {
            "servicio_id": servicio_id,
            "contacto_id": cuerpo.contacto_id,
            "contacto_nombre": contacto.data[0]["nombre"],
            "agregado_por_email": usuario["email"],
            "agregado_por_nombre": usuario["nombre"],
        }
        respuesta = supabase.table('servicio_acompanantes').insert(datos).execute()
        _sincronizar_calendario_servicio(servicio_id)
        await manager.broadcast("update")
        return {"mensaje": "Asignado", "datos": respuesta.data[0]}
    except HTTPException: raise
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=400, detail="Esa persona ya está asignada a este servicio.")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/operaciones/acompanantes/{servicio_id}/{acompanante_id}")
async def eliminar_acompanante(servicio_id: str, acompanante_id: str, usuario: dict = Depends(requiere_rol("admin", "jefe_operaciones"))):
    try:
        _verificar_servicio_no_liberado(servicio_id)
        supabase.table('servicio_acompanantes').delete().eq('id', acompanante_id).execute()
        _sincronizar_calendario_servicio(servicio_id)
        await manager.broadcast("update")
        return {"mensaje": "Quitado de la asignación"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# --- OPERACIONES: OBSERVACIONES OPERATIVAS ---
# Historial de notas con fecha, se van acumulando. Las escribe el jefe_operaciones (o admin).
# ============================================================
@app.get("/operaciones/observaciones/{servicio_id}")
async def obtener_observaciones(servicio_id: str, usuario: dict = Depends(obtener_usuario_actual)):
    respuesta = supabase.table('observaciones_operativas').select("*").eq('servicio_id', servicio_id).order('fecha', desc=True).execute()
    return respuesta.data

@app.post("/operaciones/observaciones/{servicio_id}")
async def agregar_observacion(servicio_id: str, cuerpo: ObservacionNueva, usuario: dict = Depends(requiere_rol("admin", "jefe_operaciones"))):
    try:
        _verificar_servicio_no_liberado(servicio_id)
        datos = {
            "servicio_id": servicio_id,
            "texto": cuerpo.texto,
            "autor_email": usuario["email"],
            "autor_nombre": usuario["nombre"],
        }
        respuesta = supabase.table('observaciones_operativas').insert(datos).execute()
        await manager.broadcast("update")
        return {"mensaje": "Observación agregada", "datos": respuesta.data[0]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# --- ALMACÉN: inventario, solicitudes con aprobación y préstamos ---
# Reglas acordadas con el jefe:
#  - Cualquier usuario puede solicitar material.
#  - Lo aprueba su jefe directo (organigrama en roles.jefe_directo_email; si está vacío,
#    se usa el rol: operador/auxiliar -> jefe_operaciones, los demás -> admin) y la
#    aprobación FINAL es de Finanzas (rol financiero). El admin puede todo.
#  - Operaciones (jefe_operaciones y admin) entrega y recibe el material.
#  - Finanzas captura artículos, entradas y precios. "Valor del almacén" = existencias x precio.
#  - Tipo 'consumible' sale y no regresa; tipo 'retornable' se presta y debe regresar
#    (con historial de quién lo tuvo; se puede marcar como extraviado).
# ============================================================
ROLES_FINANZAS_ALMACEN = ("admin", "financiero")
ROLES_ENTREGA_ALMACEN = ("admin", "jefe_operaciones")
ROLES_VEN_HISTORIAL_ALMACEN = ("admin", "financiero", "jefe_operaciones")
TIPOS_ARTICULO_ALMACEN = ("consumible", "retornable")

class ArticuloNuevo(BaseModel):
    nombre: str
    descripcion: Optional[str] = ""
    tipo: Optional[str] = "consumible"
    unidad: Optional[str] = "pza"
    cantidad_inicial: Optional[float] = 0
    precio_unitario: Optional[float] = None

class ArticuloEditar(BaseModel):
    nombre: Optional[str] = None
    descripcion: Optional[str] = None
    tipo: Optional[str] = None
    unidad: Optional[str] = None
    precio_unitario: Optional[float] = None
    quitar_precio: Optional[bool] = False
    activo: Optional[bool] = None

class EntradaAlmacen(BaseModel):
    cantidad: float
    precio_unitario: Optional[float] = None
    nota: Optional[str] = ""

class BajaAlmacen(BaseModel):
    cantidad: float
    nota: Optional[str] = ""

class SolicitudAlmacenNueva(BaseModel):
    articulo_id: Optional[str] = None
    nombre_solicitado: Optional[str] = ""
    cantidad: float
    motivo: Optional[str] = ""
    servicio_id: Optional[str] = None

class RechazoAlmacen(BaseModel):
    motivo: Optional[str] = ""

class VincularArticuloAlmacen(BaseModel):
    articulo_id: Optional[str] = None
    crear: Optional[ArticuloNuevo] = None

class RegresoPrestamoAlmacen(BaseModel):
    cantidad: Optional[float] = None
    nota: Optional[str] = ""

def _ahora_iso():
    return datetime.utcnow().isoformat()

def _num(valor, defecto=0.0):
    try:
        return float(valor) if valor is not None else defecto
    except Exception:
        return defecto

def _mismo_correo(a, b):
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    return bool(a) and a == b

def _jefe_de(solicitante_email: str, solicitante_rol: str):
    """Regresa (jefe_directo_email, jefe_rol, estado_inicial) según el organigrama o, si no hay, el rol."""
    email_jefe = None
    try:
        filas = supabase.table('roles').select("email, jefe_directo_email").execute().data or []
        for f in filas:
            if _mismo_correo(f.get("email"), solicitante_email):
                email_jefe = (f.get("jefe_directo_email") or "").strip() or None
                break
    except Exception:
        # Si la columna aún no existe (migración 9 sin correr) usamos solo la regla por rol
        logger.error("No se pudo leer roles.jefe_directo_email: %s", traceback.format_exc())
    if solicitante_rol == "admin":
        return None, None, "pendiente_final"          # el admin no necesita aprobación de jefe
    if solicitante_rol in ("operador", "auxiliar_del_auxiliar"):
        return email_jefe, "jefe_operaciones", "pendiente_jefe"
    return email_jefe, "admin", "pendiente_jefe"

def _es_jefe_designado(usuario: dict, sol: dict) -> bool:
    if sol.get("jefe_directo_email"):
        return _mismo_correo(usuario["email"], sol["jefe_directo_email"])
    return bool(sol.get("jefe_rol")) and usuario["rol"] == sol["jefe_rol"]

def _puede_aprobar_jefe(usuario: dict, sol: dict) -> bool:
    if sol.get("estado") != "pendiente_jefe":
        return False
    if usuario["rol"] == "admin":
        return True
    if _mismo_correo(usuario["email"], sol.get("solicitante_email")):
        return False
    return _es_jefe_designado(usuario, sol)

def _puede_aprobar_final(usuario: dict, sol: dict) -> bool:
    if sol.get("estado") != "pendiente_final":
        return False
    if usuario["rol"] == "admin":
        return True
    return usuario["rol"] == "financiero" and not _mismo_correo(usuario["email"], sol.get("solicitante_email"))

def _articulo_o_404(articulo_id) -> dict:
    r = supabase.table('almacen_articulos').select("*").eq('id', articulo_id).execute()
    if not r.data:
        raise HTTPException(status_code=404, detail="Artículo no encontrado.")
    return r.data[0]

def _en_prestamo_por_articulo() -> dict:
    filas = supabase.table('almacen_prestamos').select("articulo_id, cantidad_pendiente, estado").execute().data or []
    totales = {}
    for f in filas:
        if f.get("estado") == "abierto":
            k = str(f["articulo_id"])
            totales[k] = totales.get(k, 0) + _num(f.get("cantidad_pendiente"))
    return totales

def _disponible_de(articulo: dict) -> float:
    en_prestamo = _en_prestamo_por_articulo().get(str(articulo["id"]), 0)
    return _num(articulo.get("cantidad_total")) - en_prestamo

def _registrar_movimiento(articulo_id, tipo, cantidad, usuario, precio=None, solicitud_id=None, servicio_id=None,
                          prestamo_id=None, persona_email=None, persona_nombre=None, nota=""):
    supabase.table('almacen_movimientos').insert({
        "articulo_id": articulo_id, "tipo": tipo, "cantidad": cantidad, "precio_unitario": precio,
        "solicitud_id": solicitud_id, "servicio_id": servicio_id, "prestamo_id": prestamo_id,
        "persona_email": persona_email, "persona_nombre": persona_nombre,
        "registrado_por_email": usuario["email"], "registrado_por_nombre": usuario["nombre"],
        "nota": nota or ""
    }).execute()

def _aplicar_entrada(articulo: dict, cantidad: float, precio, usuario: dict, nota: str = ""):
    """Suma existencias y recalcula el costo promedio si viene precio."""
    stock = _num(articulo.get("cantidad_total"))
    precio_ant = articulo.get("precio_unitario")
    nuevo_total = stock + cantidad
    nuevo_precio = precio_ant
    if precio is not None:
        if precio_ant is not None and stock > 0:
            nuevo_precio = round((stock * _num(precio_ant) + cantidad * precio) / nuevo_total, 4)
        else:
            nuevo_precio = precio
    supabase.table('almacen_articulos').update({
        "cantidad_total": nuevo_total, "precio_unitario": nuevo_precio, "actualizado_en": _ahora_iso()
    }).eq('id', articulo["id"]).execute()
    _registrar_movimiento(articulo["id"], "entrada", cantidad, usuario, precio=precio, nota=nota)

def _crear_articulo(datos: ArticuloNuevo, usuario: dict) -> dict:
    nombre = (datos.nombre or "").strip()
    if not nombre:
        raise HTTPException(status_code=400, detail="El artículo necesita un nombre.")
    tipo = (datos.tipo or "consumible").strip().lower()
    if tipo not in TIPOS_ARTICULO_ALMACEN:
        raise HTTPException(status_code=400, detail="El tipo debe ser 'consumible' o 'retornable'.")
    if _num(datos.cantidad_inicial) < 0 or (datos.precio_unitario is not None and datos.precio_unitario < 0):
        raise HTTPException(status_code=400, detail="La cantidad y el precio no pueden ser negativos.")
    existentes = supabase.table('almacen_articulos').select("id, nombre").execute().data or []
    if any((e.get("nombre") or "").strip().lower() == nombre.lower() for e in existentes):
        raise HTTPException(status_code=400, detail="Ya existe un artículo con ese nombre.")
    creado = supabase.table('almacen_articulos').insert({
        "nombre": nombre, "descripcion": datos.descripcion or "", "tipo": tipo,
        "unidad": (datos.unidad or "pza").strip() or "pza", "cantidad_total": 0,
        "precio_unitario": None, "activo": True, "creado_por_email": usuario["email"]
    }).execute().data[0]
    if _num(datos.cantidad_inicial) > 0:
        _aplicar_entrada(creado, _num(datos.cantidad_inicial), datos.precio_unitario, usuario, "Inventario inicial")
        creado = _articulo_o_404(creado["id"])
    elif datos.precio_unitario is not None:
        supabase.table('almacen_articulos').update({"precio_unitario": datos.precio_unitario}).eq('id', creado["id"]).execute()
        creado = _articulo_o_404(creado["id"])
    return creado

@app.get("/almacen/yo")
async def almacen_yo(usuario: dict = Depends(obtener_usuario_actual)):
    return {"email": usuario["email"], "nombre": usuario["nombre"], "rol": usuario["rol"]}

@app.get("/almacen/articulos")
async def listar_articulos_almacen(usuario: dict = Depends(obtener_usuario_actual)):
    try:
        articulos = supabase.table('almacen_articulos').select("*").execute().data or []
        prestamos = _en_prestamo_por_articulo()
        ve_precios = usuario["rol"] in ROLES_FINANZAS_ALMACEN
        salida = []
        for a in sorted(articulos, key=lambda x: (x.get("nombre") or "").lower()):
            total = _num(a.get("cantidad_total"))
            en_prestamo = prestamos.get(str(a["id"]), 0)
            fila = {
                "id": a["id"], "nombre": a.get("nombre"), "descripcion": a.get("descripcion") or "",
                "tipo": a.get("tipo"), "unidad": a.get("unidad") or "pza", "activo": a.get("activo", True),
                "cantidad_total": total, "en_prestamo": en_prestamo, "disponible": total - en_prestamo
            }
            if ve_precios:
                precio = a.get("precio_unitario")
                fila["precio_unitario"] = precio
                fila["valor"] = round(total * _num(precio), 2) if precio is not None else None
            salida.append(fila)
        return salida
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/almacen/articulos")
async def crear_articulo_almacen(datos: ArticuloNuevo, usuario: dict = Depends(requiere_rol(*ROLES_FINANZAS_ALMACEN))):
    try:
        creado = _crear_articulo(datos, usuario)
        await manager.broadcast("update")
        return {"mensaje": "Artículo creado", "datos": creado}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.put("/almacen/articulos/{articulo_id}")
async def editar_articulo_almacen(articulo_id: str, datos: ArticuloEditar, usuario: dict = Depends(requiere_rol(*ROLES_FINANZAS_ALMACEN))):
    try:
        art = _articulo_o_404(articulo_id)
        cambios = {}
        if datos.nombre is not None:
            nombre = datos.nombre.strip()
            if not nombre:
                raise HTTPException(status_code=400, detail="El nombre no puede quedar vacío.")
            otros = supabase.table('almacen_articulos').select("id, nombre").execute().data or []
            if any(str(o["id"]) != str(articulo_id) and (o.get("nombre") or "").strip().lower() == nombre.lower() for o in otros):
                raise HTTPException(status_code=400, detail="Ya existe otro artículo con ese nombre.")
            cambios["nombre"] = nombre
        if datos.descripcion is not None: cambios["descripcion"] = datos.descripcion
        if datos.unidad is not None: cambios["unidad"] = datos.unidad.strip() or "pza"
        if datos.tipo is not None:
            tipo = datos.tipo.strip().lower()
            if tipo not in TIPOS_ARTICULO_ALMACEN:
                raise HTTPException(status_code=400, detail="El tipo debe ser 'consumible' o 'retornable'.")
            if tipo != art.get("tipo") and _en_prestamo_por_articulo().get(str(articulo_id), 0) > 0:
                raise HTTPException(status_code=400, detail="No se puede cambiar el tipo mientras haya préstamos abiertos de este artículo.")
            cambios["tipo"] = tipo
        if datos.quitar_precio:
            cambios["precio_unitario"] = None
        elif datos.precio_unitario is not None:
            if datos.precio_unitario < 0:
                raise HTTPException(status_code=400, detail="El precio no puede ser negativo.")
            cambios["precio_unitario"] = datos.precio_unitario
        if datos.activo is not None: cambios["activo"] = datos.activo
        if not cambios:
            return {"mensaje": "Sin cambios"}
        cambios["actualizado_en"] = _ahora_iso()
        supabase.table('almacen_articulos').update(cambios).eq('id', articulo_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Artículo actualizado"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/almacen/articulos/{articulo_id}/entrada")
async def entrada_articulo_almacen(articulo_id: str, datos: EntradaAlmacen, usuario: dict = Depends(requiere_rol(*ROLES_FINANZAS_ALMACEN))):
    try:
        if datos.cantidad <= 0:
            raise HTTPException(status_code=400, detail="La cantidad debe ser mayor a cero.")
        if datos.precio_unitario is not None and datos.precio_unitario < 0:
            raise HTTPException(status_code=400, detail="El precio no puede ser negativo.")
        art = _articulo_o_404(articulo_id)
        _aplicar_entrada(art, datos.cantidad, datos.precio_unitario, usuario, datos.nota or "")
        await manager.broadcast("update")
        return {"mensaje": "Entrada registrada"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/almacen/articulos/{articulo_id}/baja")
async def baja_articulo_almacen(articulo_id: str, datos: BajaAlmacen, usuario: dict = Depends(requiere_rol(*ROLES_FINANZAS_ALMACEN))):
    """Baja manual (merma, corrección de conteo): resta existencias y baja el valor del almacén."""
    try:
        if datos.cantidad <= 0:
            raise HTTPException(status_code=400, detail="La cantidad debe ser mayor a cero.")
        art = _articulo_o_404(articulo_id)
        if datos.cantidad > _disponible_de(art):
            raise HTTPException(status_code=400, detail="No hay esa cantidad disponible (lo que está prestado no se puede dar de baja aquí).")
        supabase.table('almacen_articulos').update({
            "cantidad_total": _num(art.get("cantidad_total")) - datos.cantidad, "actualizado_en": _ahora_iso()
        }).eq('id', articulo_id).execute()
        _registrar_movimiento(articulo_id, "baja", datos.cantidad, usuario, precio=art.get("precio_unitario"), nota=datos.nota or "")
        await manager.broadcast("update")
        return {"mensaje": "Baja registrada"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ---------- Solicitudes ----------
def _enriquecer_solicitudes(solicitudes: list, usuario: dict) -> list:
    articulos = {str(a["id"]): a for a in (supabase.table('almacen_articulos').select("*").execute().data or [])}
    prestamos = _en_prestamo_por_articulo()
    salida = []
    for s in solicitudes:
        art = articulos.get(str(s.get("articulo_id"))) if s.get("articulo_id") else None
        disponible = (_num(art.get("cantidad_total")) - prestamos.get(str(art["id"]), 0)) if art else None
        fila = dict(s)
        fila["articulo_nombre"] = art.get("nombre") if art else (s.get("nombre_solicitado") or "")
        fila["articulo_tipo"] = art.get("tipo") if art else None
        fila["articulo_unidad"] = (art.get("unidad") or "pza") if art else ""
        fila["disponible"] = disponible
        fila["sin_existencia"] = (art is None) or (disponible is not None and disponible < _num(s.get("cantidad")))
        fila["puede_aprobar_jefe"] = _puede_aprobar_jefe(usuario, s)
        fila["puede_aprobar_final"] = _puede_aprobar_final(usuario, s)
        fila["puede_entregar"] = usuario["rol"] in ROLES_ENTREGA_ALMACEN and s.get("estado") == "aprobada"
        fila["puede_vincular"] = usuario["rol"] in ROLES_FINANZAS_ALMACEN and s.get("estado") in ("pendiente_final", "aprobada") and not s.get("articulo_id")
        fila["puede_cancelar"] = (_mismo_correo(usuario["email"], s.get("solicitante_email")) or usuario["rol"] == "admin") and s.get("estado") in ("pendiente_jefe", "pendiente_final", "aprobada")
        fila["puede_enterado"] = _mismo_correo(usuario["email"], s.get("solicitante_email")) and s.get("notificado_solicitante") is False
        salida.append(fila)
    salida.sort(key=lambda x: x.get("creado_en") or "", reverse=True)
    return salida

def _solicitudes_visibles(usuario: dict) -> list:
    todas = supabase.table('almacen_solicitudes').select("*").execute().data or []
    if usuario["rol"] in ROLES_VEN_HISTORIAL_ALMACEN:
        visibles = todas
    else:
        visibles = [s for s in todas if _mismo_correo(usuario["email"], s.get("solicitante_email")) or _es_jefe_designado(usuario, s)]
    return _enriquecer_solicitudes(visibles, usuario)

def _solicitud_o_404(solicitud_id) -> dict:
    r = supabase.table('almacen_solicitudes').select("*").eq('id', solicitud_id).execute()
    if not r.data:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada.")
    return r.data[0]

@app.get("/almacen/solicitudes")
async def listar_solicitudes_almacen(usuario: dict = Depends(obtener_usuario_actual)):
    try:
        return _solicitudes_visibles(usuario)
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/almacen/solicitudes")
async def crear_solicitud_almacen(datos: SolicitudAlmacenNueva, usuario: dict = Depends(obtener_usuario_actual)):
    try:
        if datos.cantidad <= 0:
            raise HTTPException(status_code=400, detail="La cantidad debe ser mayor a cero.")
        articulo_id = (datos.articulo_id or "").strip() or None
        nombre = (datos.nombre_solicitado or "").strip()
        if not articulo_id and not nombre:
            raise HTTPException(status_code=400, detail="Elige un artículo del almacén o escribe qué material necesitas.")
        if articulo_id:
            art = _articulo_o_404(articulo_id)
            if art.get("activo") is False:
                raise HTTPException(status_code=400, detail="Ese artículo está archivado.")
        jefe_email, jefe_rol, estado = _jefe_de(usuario["email"], usuario["rol"])
        creada = supabase.table('almacen_solicitudes').insert({
            "articulo_id": articulo_id, "nombre_solicitado": "" if articulo_id else nombre,
            "cantidad": datos.cantidad, "motivo": datos.motivo or "",
            "servicio_id": (datos.servicio_id or "").strip() or None,
            "solicitante_email": usuario["email"], "solicitante_nombre": usuario["nombre"], "solicitante_rol": usuario["rol"],
            "jefe_directo_email": jefe_email, "jefe_rol": jefe_rol, "estado": estado
        }).execute().data[0]
        await manager.broadcast("update")
        return {"mensaje": "Solicitud enviada", "datos": creada}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/almacen/solicitudes/{solicitud_id}/aprobar-jefe")
async def aprobar_solicitud_jefe(solicitud_id: str, usuario: dict = Depends(obtener_usuario_actual)):
    try:
        sol = _solicitud_o_404(solicitud_id)
        if not _puede_aprobar_jefe(usuario, sol):
            raise HTTPException(status_code=403, detail="No puedes aprobar esta solicitud en este paso.")
        supabase.table('almacen_solicitudes').update({
            "estado": "pendiente_final", "aprobado_jefe_por": usuario["nombre"], "aprobado_jefe_en": _ahora_iso()
        }).eq('id', solicitud_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Aprobada por el jefe directo, pasa a Finanzas"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/almacen/solicitudes/{solicitud_id}/aprobar-final")
async def aprobar_solicitud_final(solicitud_id: str, usuario: dict = Depends(obtener_usuario_actual)):
    try:
        sol = _solicitud_o_404(solicitud_id)
        if not _puede_aprobar_final(usuario, sol):
            raise HTTPException(status_code=403, detail="No puedes dar la aprobación final de esta solicitud.")
        if not sol.get("articulo_id"):
            raise HTTPException(status_code=400, detail="Antes de aprobar, vincula la solicitud a un artículo del almacén (o crea uno nuevo).")
        supabase.table('almacen_solicitudes').update({
            "estado": "aprobada", "aprobado_final_por": usuario["nombre"], "aprobado_final_en": _ahora_iso(),
            "notificado_solicitante": False
        }).eq('id', solicitud_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Solicitud aprobada"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/almacen/solicitudes/{solicitud_id}/rechazar")
async def rechazar_solicitud_almacen(solicitud_id: str, cuerpo: RechazoAlmacen, usuario: dict = Depends(obtener_usuario_actual)):
    try:
        sol = _solicitud_o_404(solicitud_id)
        if not (_puede_aprobar_jefe(usuario, sol) or _puede_aprobar_final(usuario, sol)):
            raise HTTPException(status_code=403, detail="No puedes rechazar esta solicitud en este paso.")
        supabase.table('almacen_solicitudes').update({
            "estado": "rechazada", "rechazado_por": usuario["nombre"], "rechazado_en": _ahora_iso(),
            "motivo_rechazo": cuerpo.motivo or "", "notificado_solicitante": False
        }).eq('id', solicitud_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Solicitud rechazada"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/almacen/solicitudes/{solicitud_id}/cancelar")
async def cancelar_solicitud_almacen(solicitud_id: str, usuario: dict = Depends(obtener_usuario_actual)):
    try:
        sol = _solicitud_o_404(solicitud_id)
        if not (_mismo_correo(usuario["email"], sol.get("solicitante_email")) or usuario["rol"] == "admin"):
            raise HTTPException(status_code=403, detail="Solo quien la pidió (o el admin) puede cancelarla.")
        if sol.get("estado") not in ("pendiente_jefe", "pendiente_final", "aprobada"):
            raise HTTPException(status_code=400, detail="Esta solicitud ya no se puede cancelar.")
        supabase.table('almacen_solicitudes').update({"estado": "cancelada"}).eq('id', solicitud_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Solicitud cancelada"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/almacen/solicitudes/{solicitud_id}/enterado")
async def solicitud_enterado(solicitud_id: str, usuario: dict = Depends(obtener_usuario_actual)):
    try:
        sol = _solicitud_o_404(solicitud_id)
        if not _mismo_correo(usuario["email"], sol.get("solicitante_email")):
            raise HTTPException(status_code=403, detail="Solo quien la pidió puede marcarla como vista.")
        supabase.table('almacen_solicitudes').update({"notificado_solicitante": True}).eq('id', solicitud_id).execute()
        return {"mensaje": "Listo"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/almacen/solicitudes/{solicitud_id}/vincular-articulo")
async def vincular_articulo_solicitud(solicitud_id: str, cuerpo: VincularArticuloAlmacen, usuario: dict = Depends(requiere_rol(*ROLES_FINANZAS_ALMACEN))):
    """Para solicitudes de material que no estaba en el catálogo: Finanzas lo liga a uno existente o crea el artículo."""
    try:
        sol = _solicitud_o_404(solicitud_id)
        if sol.get("estado") not in ("pendiente_final", "aprobada"):
            raise HTTPException(status_code=400, detail="Solo se puede vincular cuando la solicitud está en revisión de Finanzas o aprobada.")
        if sol.get("articulo_id"):
            raise HTTPException(status_code=400, detail="Esta solicitud ya está ligada a un artículo.")
        if cuerpo.articulo_id:
            art = _articulo_o_404(cuerpo.articulo_id)
        elif cuerpo.crear:
            art = _crear_articulo(cuerpo.crear, usuario)
        else:
            raise HTTPException(status_code=400, detail="Elige un artículo existente o crea uno nuevo.")
        supabase.table('almacen_solicitudes').update({"articulo_id": art["id"]}).eq('id', solicitud_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Solicitud ligada al artículo", "articulo_id": art["id"]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/almacen/solicitudes/{solicitud_id}/entregar")
async def entregar_solicitud_almacen(solicitud_id: str, usuario: dict = Depends(requiere_rol(*ROLES_ENTREGA_ALMACEN))):
    """Operaciones entrega el material: consumible -> sale y baja el valor; retornable -> queda en préstamo."""
    try:
        sol = _solicitud_o_404(solicitud_id)
        if sol.get("estado") != "aprobada":
            raise HTTPException(status_code=400, detail="Esta solicitud todavía no tiene la aprobación final de Finanzas.")
        if not sol.get("articulo_id"):
            raise HTTPException(status_code=400, detail="La solicitud no está ligada a un artículo del almacén.")
        art = _articulo_o_404(sol["articulo_id"])
        cantidad = _num(sol.get("cantidad"))
        disponible = _disponible_de(art)
        if cantidad > disponible:
            raise HTTPException(status_code=400, detail=f"No hay existencia suficiente (disponible: {disponible:g} {art.get('unidad') or 'pza'}). Finanzas debe registrar la compra primero.")

        ahora = _ahora_iso()
        if art.get("tipo") == "retornable":
            prestamo = supabase.table('almacen_prestamos').insert({
                "articulo_id": art["id"], "solicitud_id": solicitud_id, "servicio_id": sol.get("servicio_id"),
                "persona_email": sol.get("solicitante_email"), "persona_nombre": sol.get("solicitante_nombre"),
                "cantidad": cantidad, "cantidad_pendiente": cantidad, "estado": "abierto",
                "entregado_por_email": usuario["email"], "entregado_por_nombre": usuario["nombre"], "entregado_en": ahora
            }).execute().data[0]
            _registrar_movimiento(art["id"], "prestamo", cantidad, usuario, solicitud_id=solicitud_id, servicio_id=sol.get("servicio_id"),
                                  prestamo_id=prestamo["id"], persona_email=sol.get("solicitante_email"), persona_nombre=sol.get("solicitante_nombre"))
        else:
            supabase.table('almacen_articulos').update({
                "cantidad_total": _num(art.get("cantidad_total")) - cantidad, "actualizado_en": ahora
            }).eq('id', art["id"]).execute()
            _registrar_movimiento(art["id"], "salida", cantidad, usuario, precio=art.get("precio_unitario"), solicitud_id=solicitud_id,
                                  servicio_id=sol.get("servicio_id"), persona_email=sol.get("solicitante_email"), persona_nombre=sol.get("solicitante_nombre"))

        supabase.table('almacen_solicitudes').update({
            "estado": "entregada", "entregado_por": usuario["nombre"], "entregado_en": ahora, "notificado_solicitante": False
        }).eq('id', solicitud_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Material entregado"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ---------- Préstamos (artículos retornables) ----------
@app.get("/almacen/prestamos")
async def listar_prestamos_almacen(usuario: dict = Depends(requiere_rol(*ROLES_VEN_HISTORIAL_ALMACEN))):
    try:
        articulos = {str(a["id"]): a for a in (supabase.table('almacen_articulos').select("id, nombre, unidad").execute().data or [])}
        filas = supabase.table('almacen_prestamos').select("*").execute().data or []
        salida = []
        for p in filas:
            fila = dict(p)
            art = articulos.get(str(p.get("articulo_id"))) or {}
            fila["articulo_nombre"] = art.get("nombre") or "(artículo eliminado)"
            fila["articulo_unidad"] = art.get("unidad") or "pza"
            fila["puede_registrar"] = usuario["rol"] in ROLES_ENTREGA_ALMACEN and p.get("estado") == "abierto"
            salida.append(fila)
        salida.sort(key=lambda x: x.get("entregado_en") or "", reverse=True)
        return salida
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

def _cerrar_parcial_prestamo(prestamo_id: str, cuerpo: RegresoPrestamoAlmacen, usuario: dict, tipo_mov: str):
    r = supabase.table('almacen_prestamos').select("*").eq('id', prestamo_id).execute()
    if not r.data:
        raise HTTPException(status_code=404, detail="Préstamo no encontrado.")
    p = r.data[0]
    if p.get("estado") != "abierto":
        raise HTTPException(status_code=400, detail="Este préstamo ya está cerrado.")
    pendiente = _num(p.get("cantidad_pendiente"))
    cantidad = _num(cuerpo.cantidad, pendiente) if cuerpo.cantidad is not None else pendiente
    if cantidad <= 0 or cantidad > pendiente:
        raise HTTPException(status_code=400, detail=f"La cantidad debe ser mayor a cero y no pasar de lo pendiente ({pendiente:g}).")
    art = _articulo_o_404(p["articulo_id"])
    nuevo_pendiente = pendiente - cantidad
    cambios = {"cantidad_pendiente": nuevo_pendiente}
    if nuevo_pendiente <= 0:
        cambios["estado"] = "cerrado"
        cambios["cerrado_en"] = _ahora_iso()
    supabase.table('almacen_prestamos').update(cambios).eq('id', prestamo_id).execute()
    if tipo_mov == "extravio":
        # Lo extraviado sale del inventario (y baja el valor del almacén)
        supabase.table('almacen_articulos').update({
            "cantidad_total": _num(art.get("cantidad_total")) - cantidad, "actualizado_en": _ahora_iso()
        }).eq('id', art["id"]).execute()
    _registrar_movimiento(art["id"], tipo_mov, cantidad, usuario, precio=art.get("precio_unitario") if tipo_mov == "extravio" else None,
                          solicitud_id=p.get("solicitud_id"), servicio_id=p.get("servicio_id"), prestamo_id=prestamo_id,
                          persona_email=p.get("persona_email"), persona_nombre=p.get("persona_nombre"), nota=cuerpo.nota or "")

@app.post("/almacen/prestamos/{prestamo_id}/regreso")
async def registrar_regreso_prestamo(prestamo_id: str, cuerpo: RegresoPrestamoAlmacen, usuario: dict = Depends(requiere_rol(*ROLES_ENTREGA_ALMACEN))):
    try:
        _cerrar_parcial_prestamo(prestamo_id, cuerpo, usuario, "regreso")
        await manager.broadcast("update")
        return {"mensaje": "Regreso registrado"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/almacen/prestamos/{prestamo_id}/extravio")
async def registrar_extravio_prestamo(prestamo_id: str, cuerpo: RegresoPrestamoAlmacen, usuario: dict = Depends(requiere_rol(*ROLES_ENTREGA_ALMACEN))):
    try:
        _cerrar_parcial_prestamo(prestamo_id, cuerpo, usuario, "extravio")
        await manager.broadcast("update")
        return {"mensaje": "Marcado como extraviado y descontado del inventario"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ---------- Historial ----------
@app.get("/almacen/movimientos")
async def listar_movimientos_almacen(articulo_id: Optional[str] = None, usuario: dict = Depends(requiere_rol(*ROLES_VEN_HISTORIAL_ALMACEN))):
    try:
        articulos = {str(a["id"]): a for a in (supabase.table('almacen_articulos').select("id, nombre, unidad").execute().data or [])}
        filas = supabase.table('almacen_movimientos').select("*").execute().data or []
        if articulo_id:
            filas = [m for m in filas if str(m.get("articulo_id")) == str(articulo_id)]
        filas.sort(key=lambda x: x.get("creado_en") or "", reverse=True)
        ve_precios = usuario["rol"] in ROLES_FINANZAS_ALMACEN
        salida = []
        for m in filas[:500]:
            fila = dict(m)
            art = articulos.get(str(m.get("articulo_id"))) or {}
            fila["articulo_nombre"] = art.get("nombre") or "(artículo eliminado)"
            fila["articulo_unidad"] = art.get("unidad") or "pza"
            if not ve_precios:
                fila.pop("precio_unitario", None)
            salida.append(fila)
        return salida
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# Material de almacén usado en un servicio (fase 3: ligar consumos a un servicio)
@app.get("/almacen/servicio/{servicio_id}/materiales")
async def materiales_de_servicio(servicio_id: str, usuario: dict = Depends(obtener_usuario_actual)):
    try:
        articulos = {str(a["id"]): a for a in (supabase.table('almacen_articulos').select("id, nombre, unidad, tipo").execute().data or [])}
        filas = supabase.table('almacen_movimientos').select("*").eq('servicio_id', servicio_id).execute().data or []
        ve_precios = usuario["rol"] in ROLES_FINANZAS_ALMACEN
        salida = []
        for m in sorted(filas, key=lambda x: x.get("creado_en") or ""):
            if m.get("tipo") not in ("salida", "prestamo"):
                continue
            art = articulos.get(str(m.get("articulo_id"))) or {}
            fila = {
                "articulo_nombre": art.get("nombre") or "(artículo eliminado)", "unidad": art.get("unidad") or "pza",
                "tipo_articulo": art.get("tipo"), "movimiento": m.get("tipo"), "cantidad": m.get("cantidad"),
                "persona_nombre": m.get("persona_nombre"), "fecha": m.get("creado_en")
            }
            if ve_precios and m.get("tipo") == "salida" and m.get("precio_unitario") is not None:
                fila["costo"] = round(_num(m.get("cantidad")) * _num(m.get("precio_unitario")), 2)
            salida.append(fila)
        return salida
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ---------- Organigrama (jefe directo de cada usuario; solo admin) ----------
class JefeDirectoCuerpo(BaseModel):
    email: str
    jefe_directo_email: Optional[str] = None

@app.get("/almacen/organigrama")
async def ver_organigrama(usuario: dict = Depends(requiere_rol("admin"))):
    try:
        filas = supabase.table('roles').select("email, nombre, rol, jefe_directo_email").execute().data or []
        filas.sort(key=lambda f: (f.get("nombre") or f.get("email") or "").lower())
        return [{"email": f.get("email"), "nombre": f.get("nombre") or f.get("email"), "rol": f.get("rol"),
                 "jefe_directo_email": f.get("jefe_directo_email") or ""} for f in filas]
    except Exception:
        logger.error("No se pudo leer el organigrama (¿corriste la migración 9?): %s", traceback.format_exc())
        raise HTTPException(status_code=500, detail="No se pudo leer el organigrama. Revisa que la migración 09 ya se haya corrido en Supabase.")

@app.put("/almacen/organigrama")
async def guardar_jefe_directo(cuerpo: JefeDirectoCuerpo, usuario: dict = Depends(requiere_rol("admin"))):
    try:
        filas = supabase.table('roles').select("email").execute().data or []
        persona = next((f for f in filas if _mismo_correo(f.get("email"), cuerpo.email)), None)
        if not persona:
            raise HTTPException(status_code=404, detail="No encontré a ese usuario.")
        jefe_correo = (cuerpo.jefe_directo_email or "").strip()
        jefe_guardado = None
        if jefe_correo:
            jefe = next((f for f in filas if _mismo_correo(f.get("email"), jefe_correo)), None)
            if not jefe:
                raise HTTPException(status_code=404, detail="No encontré al jefe directo elegido.")
            if _mismo_correo(jefe.get("email"), persona.get("email")):
                raise HTTPException(status_code=400, detail="Una persona no puede ser su propio jefe directo.")
            jefe_guardado = jefe.get("email")
        supabase.table('roles').update({"jefe_directo_email": jefe_guardado}).eq('email', persona["email"]).execute()
        await manager.broadcast("update")
        return {"mensaje": "Jefe directo actualizado"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# Pendientes del usuario para la campanita
@app.get("/almacen/mis-pendientes")
async def almacen_mis_pendientes(usuario: dict = Depends(obtener_usuario_actual)):
    try:
        solicitudes = _solicitudes_visibles(usuario)
        resumen = lambda s: {
            "id": s["id"], "articulo_nombre": s.get("articulo_nombre"), "cantidad": s.get("cantidad"),
            "unidad": s.get("articulo_unidad"), "solicitante_nombre": s.get("solicitante_nombre"),
            "estado": s.get("estado"), "motivo_rechazo": s.get("motivo_rechazo")
        }
        return {
            "por_aprobar": [resumen(s) for s in solicitudes if s["puede_aprobar_jefe"] or s["puede_aprobar_final"]],
            "por_entregar": [resumen(s) for s in solicitudes if s["puede_entregar"]],
            "por_comprar": [resumen(s) for s in solicitudes if usuario["rol"] in ROLES_FINANZAS_ALMACEN and s.get("estado") == "aprobada" and s.get("sin_existencia")],
            "respuestas": [resumen(s) for s in solicitudes if s["puede_enterado"]],
        }
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    