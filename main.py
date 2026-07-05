from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from supabase import create_client, Client
import os
from dotenv import load_dotenv

# --- NUEVAS IMPORTACIONES PARA GOOGLE CALENDAR ---
from google.oauth2 import service_account
from googleapiclient.discovery import build

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
def agendar_en_google_calendar(fecha, no_cotizacion, observaciones, nombre_productor, parcela):
    # ¡IMPORTANTE! Cambia esto por el correo dueño del calendario
    CORREO_CALENDARIO = 'facturacion@asoa.com.mx' 
    ARCHIVO_CREDENCIALES = 'credenciales_calendario.json'
    
    # --- AQUÍ PONES LOS CORREOS DE TU EQUIPO ---
    correos_equipo = [
        {'email': 'piloto1@gmail.com'},
        {'email': 'tecnico@asoa.com.mx'},
        {'email': 'otro_companero@gmail.com'}
    ]
    
    if not os.path.exists(ARCHIVO_CREDENCIALES):
        print("No se encontró la llave del calendario (JSON).")
        return None

    try:
        creds = service_account.Credentials.from_service_account_file(
            ARCHIVO_CREDENCIALES, scopes=['https://www.googleapis.com/auth/calendar'])
        servicio = build('calendar', 'v3', credentials=creds)
        
        evento = {
            'summary': f'🚜 Vuelo: {nombre_productor}',
            'location': parcela,
            'description': f'Cotización/OS: {no_cotizacion}\nObservaciones: {observaciones}',
            'start': {'date': fecha, 'timeZone': 'America/Mexico_City'},
            'end': {'date': fecha, 'timeZone': 'America/Mexico_City'},
            'attendees': correos_equipo,  # <-- ESTO AGREGA A TU EQUIPO
        }
        
        # Insertamos y forzamos el correo de invitación
        servicio.events().insert(
            calendarId=CORREO_CALENDARIO, 
            body=evento,
            sendUpdates='all'  # <-- ESTO LES AVISA POR CORREO
        ).execute()
        
        print("¡Cita agendada y equipo notificado con éxito!")
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
        
        rol_usuario = "trabajador"
        try:
            datos_rol = supabase.table('roles').select('rol').eq('email', req.email).execute()
            if len(datos_rol.data) > 0:
                rol_usuario = datos_rol.data[0]['rol']
        except Exception:
            pass
            
        return {"mensaje": "Acceso concedido", "rol": rol_usuario}
    except Exception as e:
        raise HTTPException(status_code=401, detail="Código 2FA incorrecto o expirado.")

# --- RUTAS DE CLIENTES ---
@app.post("/clientes/")
async def registrar_cliente(cliente: ClienteNuevo):
    try:
        respuesta = supabase.table('clientes').insert(cliente.dict()).execute()
        await manager.broadcast("update") 
        return {"mensaje": "Guardado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/clientes/")
async def obtener_clientes():
    respuesta = supabase.table('clientes').select("*").execute()
    return respuesta.data

@app.put("/clientes/{cliente_id}")
async def actualizar_cliente(cliente_id: str, cliente: ClienteNuevo):
    try:
        respuesta = supabase.table('clientes').update(cliente.dict()).eq('id', cliente_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/clientes/{cliente_id}")
async def eliminar_cliente(cliente_id: str):
    try:
        supabase.table('clientes').delete().eq('id', cliente_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Eliminado exitosamente"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- RUTAS DE PROPIEDADES ---
@app.post("/propiedades/")
async def registrar_propiedad(propiedad: PropiedadNueva):
    try:
        respuesta = supabase.table('propiedades').insert(propiedad.dict()).execute()
        await manager.broadcast("update")
        return {"mensaje": "Guardado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/propiedades/")
async def obtener_propiedades():
    respuesta = supabase.table('propiedades').select("*").execute()
    return respuesta.data

@app.put("/propiedades/{propiedad_id}")
async def actualizar_propiedad(propiedad_id: str, propiedad: PropiedadNueva):
    try:
        respuesta = supabase.table('propiedades').update(propiedad.dict()).eq('id', propiedad_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/propiedades/{propiedad_id}")
async def eliminar_propiedad(propiedad_id: str):
    try:
        supabase.table('propiedades').delete().eq('id', propiedad_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Eliminado exitosamente"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- RUTAS DE SERVICIOS (MODIFICADA CON GOOGLE CALENDAR) ---
@app.post("/servicios/")
async def registrar_servicio(servicio: ServicioNuevo):
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
        
        # 3. Disparamos la agenda a Google Calendar
        fecha_cita = servicio.fecha_seguimiento or servicio.fecha_aplicacion
        if fecha_cita:
            agendar_en_google_calendar(
                fecha=fecha_cita,
                no_cotizacion=servicio.no_cotizacion or 'S/N',
                observaciones=servicio.observaciones or 'Sin detalles',
                nombre_productor=nombre_productor,
                parcela=nombre_parcela
            )
            
        return {"mensaje": "Guardado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/servicios/")
async def obtener_servicios():
    respuesta = supabase.table('servicios_aplicacion').select("*").execute()
    return respuesta.data

@app.put("/servicios/{servicio_id}")
async def actualizar_servicio(servicio_id: str, servicio: ServicioNuevo):
    try:
        respuesta = supabase.table('servicios_aplicacion').update(servicio.dict()).eq('id', servicio_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/servicios/{servicio_id}")
async def eliminar_servicio(servicio_id: str):
    try:
        supabase.table('servicios_aplicacion').delete().eq('id', servicio_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Eliminado exitosamente"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- RUTA DE BOTON "HECHO" EN SEGUIMIENTOS ---
@app.post("/api/servicios/{servicio_id}/hecho")
async def marcar_seguimiento_hecho(servicio_id: str, req: SeguimientoHecho):
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
async def registrar_cotizacion(cot: CotizacionNueva):
    try:
        respuesta = supabase.table('cotizaciones').insert(cot.dict()).execute()
        await manager.broadcast("update")
        return {"mensaje": "Cotización guardada", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/cotizaciones/")
async def obtener_cotizaciones():
    respuesta = supabase.table('cotizaciones').select("*").execute()
    return respuesta.data

@app.put("/cotizaciones/{cot_id}")
async def actualizar_cotizacion(cot_id: str, cot: CotizacionNueva):
    try:
        respuesta = supabase.table('cotizaciones').update(cot.dict()).eq('id', cot_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/cotizaciones/{cot_id}")
async def eliminar_cotizacion(cot_id: str):
    try:
        supabase.table('cotizaciones').delete().eq('id', cot_id).execute()
        await manager.broadcast("update")
        return {"mensaje": "Eliminado"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- VISITAS, METAS Y DASHBOARD ---
@app.post("/api/visitas/{mes_anio}")
async def registrar_visita(mes_anio: str):
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
async def obtener_dashboard(mes_anio: str):
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
async def guardar_metas(metas: MetasMensuales):
    try:
        existe = supabase.table('metas_mensuales').select('id').eq('mes_anio', metas.mes_anio).execute()
        if len(existe.data) > 0:
            supabase.table('metas_mensuales').update(metas.dict()).eq('mes_anio', metas.mes_anio).execute()
        else:
            supabase.table('metas_mensuales').insert(metas.dict()).execute()
        await manager.broadcast("update")
        return {"mensaje": "Metas guardadas correctamente"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))