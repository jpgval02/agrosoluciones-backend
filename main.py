from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from supabase import create_client, Client
import os
import json
import urllib.request
from dotenv import load_dotenv

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

# --- RUTA DE LOGIN ---
@app.post("/login/")
async def iniciar_sesion(credenciales: Credenciales):
    try:
        respuesta = supabase.auth.sign_in_with_password({
            "email": credenciales.email,
            "password": credenciales.password
        })
        
        token = respuesta.session.access_token
        
        headers = {
            "Authorization": f"Bearer {token}",
            "apikey": key,
            "Content-Type": "application/json"
        }
        
        try:
            req_factors = urllib.request.Request(f"{url}/auth/v1/mfa/factors", headers=headers, method="GET")
            with urllib.request.urlopen(req_factors) as res_api:
                factores_data = json.loads(res_api.read().decode())
                
            factores_verificados = [f for f in factores_data if f.get('status') == 'verified']
            factores_sucios = [f for f in factores_data if f.get('status') != 'verified']
            
            if len(factores_verificados) > 0:
                return {
                    "mensaje": "Requiere 2FA", 
                    "necesita_2fa": True, 
                    "tipo": "login",
                    "factor_id": factores_verificados[0].get("id")
                }
            else:
                for fs in factores_sucios:
                    try:
                        req_del = urllib.request.Request(f"{url}/auth/v1/mfa/factors/{fs.get('id')}", headers=headers, method="DELETE")
                        urllib.request.urlopen(req_del)
                    except: pass
                    
                # AQUI ESTÁ LA MAGIA: Forzamos el parámetro "issuer" para que diga Sistema ASOA
                payload_qr = {
                    "factor_type": "totp", 
                    "friendly_name": "Acceso Operativo",
                    "issuer": "Sistema ASOA" 
                }
                req_enroll = urllib.request.Request(f"{url}/auth/v1/mfa/factors/enroll", data=json.dumps(payload_qr).encode('utf-8'), headers=headers, method="POST")
                with urllib.request.urlopen(req_enroll) as res_enroll:
                    enroll_data = json.loads(res_enroll.read().decode())
                    
                return {
                    "mensaje": "Requiere configurar 2FA",
                    "necesita_2fa": True,
                    "tipo": "setup",
                    "factor_id": enroll_data.get("id"),
                    "qr_code": enroll_data.get("totp", {}).get("qr_code")
                }
        except Exception as api_err:
            raise HTTPException(status_code=400, detail=f"Error en comunicación con Supabase MFA: {str(api_err)}")
            
    except Exception as e:
        error_msg = str(e)
        if "Invalid login credentials" in error_msg:
            raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos.")
        elif isinstance(e, HTTPException):
            raise e
        else:
            raise HTTPException(status_code=400, detail=f"Error Crítico: {error_msg}")

# --- RUTA VERIFICACIÓN 2 PASOS ---
@app.post("/verificar-2fa/")
async def verificar_2fa(req: Verifica2FA):
    try:
        session = supabase.auth.get_session()
        if not session:
            raise Exception("Sesión expirada o no encontrada.")
            
        token = session.access_token
        headers = {
            "Authorization": f"Bearer {token}",
            "apikey": key,
            "Content-Type": "application/json"
        }
        
        req_challenge = urllib.request.Request(f"{url}/auth/v1/mfa/factors/{req.factor_id}/challenge", data=json.dumps({}).encode('utf-8'), headers=headers, method="POST")
        with urllib.request.urlopen(req_challenge) as res_c:
            challenge_data = json.loads(res_c.read().decode())
            challenge_id = challenge_data.get("id")
            
        verify_payload = {"challenge_id": challenge_id, "code": req.codigo}
        req_verify = urllib.request.Request(f"{url}/auth/v1/mfa/factors/{req.factor_id}/verify", data=json.dumps(verify_payload).encode('utf-8'), headers=headers, method="POST")
        with urllib.request.urlopen(req_verify) as res_v:
            verify_data = json.loads(res_v.read().decode())
            
        rol_usuario = "trabajador"
        try:
            datos_rol = supabase.table('roles').select('rol').eq('email', req.email).execute()
            if len(datos_rol.data) > 0:
                rol_usuario = datos_rol.data[0]['rol']
        except Exception:
            pass
            
        return {"mensaje": "Acceso concedido", "rol": rol_usuario}
    except Exception as e:
        raise HTTPException(status_code=401, detail="El código de Google Authenticator es incorrecto o ya expiró.")

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

# --- RUTAS DE SERVICIOS ---
@app.post("/servicios/")
async def registrar_servicio(servicio: ServicioNuevo):
    try:
        respuesta = supabase.table('servicios_aplicacion').insert(servicio.dict()).execute()
        try:
            prop_data = supabase.table('propiedades').select('cliente_id').eq('id', servicio.propiedad_id).execute()
            if len(prop_data.data) > 0:
                cl_id = prop_data.data[0]['cliente_id']
                supabase.table('clientes').update({'estado': 'Cliente'}).eq('id', cl_id).execute()
        except Exception:
            pass
        await manager.broadcast("update")
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