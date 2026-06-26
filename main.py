from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from supabase import create_client, Client
import os
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

# --- RUTA DE LOGIN PRINCIPAL ---
@app.post("/login/")
async def iniciar_sesion(credenciales: Credenciales):
    try:
        # Filtro 1: Validamos que exista y sea la contraseña correcta
        respuesta = supabase.auth.sign_in_with_password({
            "email": credenciales.email,
            "password": credenciales.password
        })
        
        # Filtro 2: Bóveda 2FA
        try:
            if hasattr(supabase.auth, 'mfa'):
                user_factors = getattr(respuesta.user, 'factors', []) or []
                factores_verificados = [f for f in user_factors if getattr(f, 'status', '') == 'verified']
                
                # Ya vinculó su celular anteriormente
                if len(factores_verificados) > 0:
                    factor_id = getattr(factores_verificados[0], 'id', '')
                    return {
                        "mensaje": "Requiere 2FA", 
                        "necesita_2fa": True, 
                        "tipo": "login",
                        "factor_id": factor_id
                    }
                else:
                    # Es nuevo, limpiamos basura si la hubiera y generamos QR
                    unverified = [f for f in user_factors if getattr(f, 'status', '') != 'verified']
                    for uf in unverified:
                        try: supabase.auth.mfa.unenroll(factor_id=getattr(uf, 'id', ''))
                        except: pass
                        
                    enroll_res = supabase.auth.mfa.enroll(factor_type="totp")
                    factor_id = getattr(enroll_res, 'id', '') if hasattr(enroll_res, 'id') else enroll_res.get('id', '')
                    totp = getattr(enroll_res, 'totp', None)
                    qr_code = getattr(totp, 'qr_code', '') if totp else ''
                    if not qr_code and isinstance(enroll_res, dict):
                        qr_code = enroll_res.get('totp', {}).get('qr_code', '')
                        
                    return {
                        "mensaje": "Requiere configurar 2FA",
                        "necesita_2fa": True,
                        "tipo": "setup",
                        "factor_id": factor_id,
                        "qr_code": qr_code
                    }
        except Exception as e:
            print(f"Alerta MFA Interna: {e}")
            pass 
            
        # Si por alguna razón el servidor no soporta MFA, entra de forma normal
        rol_usuario = "trabajador"
        try:
            datos_rol = supabase.table('roles').select('rol').eq('email', credenciales.email).execute()
            if len(datos_rol.data) > 0:
                rol_usuario = datos_rol.data[0]['rol']
        except Exception:
            pass
        return {"mensaje": "Acceso concedido", "rol": rol_usuario, "necesita_2fa": False}
    except Exception:
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos")

# --- RUTA VERIFICACIÓN 2 PASOS (LOS 6 NÚMEROS) ---
@app.post("/verificar-2fa/")
async def verificar_2fa(req: Verifica2FA):
    try:
        challenge = supabase.auth.mfa.challenge(factor_id=req.factor_id)
        ch_id = getattr(challenge, 'id', '') if hasattr(challenge, 'id') else challenge.get('id', '')
        
        # Validamos los 6 dígitos frente a Supabase
        verificacion = supabase.auth.mfa.verify(
            factor_id=req.factor_id, 
            challenge_id=ch_id, 
            code=req.codigo
        )
        
        rol_usuario = "trabajador"
        try:
            datos_rol = supabase.table('roles').select('rol').eq('email', req.email).execute()
            if len(datos_rol.data) > 0:
                rol_usuario = datos_rol.data[0]['rol']
        except Exception:
            pass
            
        return {"mensaje": "Acceso concedido", "rol": rol_usuario}
    except Exception as e:
        raise HTTPException(status_code=401, detail="Código 2FA incorrecto")

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