from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional  
from supabase import create_client, Client
import os
from dotenv import load_dotenv

load_dotenv()
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(url, key)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- MODELOS DE DATOS ---
class Credenciales(BaseModel):
    email: str
    password: str

class ClienteNuevo(BaseModel):
    nombre: str
    apellidos: str
    email: str
    telefono: str
    rfc: str
    estado: Optional[str] = "Prospecto"  # Campo del embudo CRM

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

class MetasMensuales(BaseModel):
    mes_anio: str
    meta_ventas: float
    meta_servicios: int
    meta_clientes: int
    meta_prospectos: int
    meta_prospectos_visitas: Optional[int] = 0

# --- RUTA DE LOGIN ---
@app.post("/login/")
async def iniciar_sesion(credenciales: Credenciales):
    try:
        respuesta = supabase.auth.sign_in_with_password({
            "email": credenciales.email,
            "password": credenciales.password
        })
        rol_usuario = "trabajador"
        try:
            datos_rol = supabase.table('roles').select('rol').eq('email', credenciales.email).execute()
            if len(datos_rol.data) > 0:
                rol_usuario = datos_rol.data[0]['rol']
        except Exception as e:
            pass
        return {"mensaje": "Acceso concedido", "rol": rol_usuario}
    except Exception as e:
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos")

# --- RUTAS DE CLIENTES ---
@app.post("/clientes/")
async def registrar_cliente(cliente: ClienteNuevo):
    try:
        respuesta = supabase.table('clientes').insert(cliente.dict()).execute()
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
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/clientes/{cliente_id}")
async def eliminar_cliente(cliente_id: str):
    try:
        supabase.table('clientes').delete().eq('id', cliente_id).execute()
        return {"mensaje": "Eliminado exitosamente"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- RUTAS DE PROPIEDADES ---
@app.post("/propiedades/")
async def registrar_propiedad(propiedad: PropiedadNueva):
    try:
        respuesta = supabase.table('propiedades').insert(propiedad.dict()).execute()
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
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/propiedades/{propiedad_id}")
async def eliminar_propiedad(propiedad_id: str):
    try:
        supabase.table('propiedades').delete().eq('id', propiedad_id).execute()
        return {"mensaje": "Eliminado exitosamente"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- RUTAS DE SERVICIOS (CON CAMBIO AUTOMÁTICO DE ESTADO CRM) ---
@app.post("/servicios/")
async def registrar_servicio(servicio: ServicioNuevo):
    try:
        respuesta = supabase.table('servicios_aplicacion').insert(servicio.dict()).execute()
        
        # TRANSICIÓN AUTOMÁTICA DEL EMBUDO: Si se genera venta, el prospecto pasa a ser Cliente Real
        try:
            prop_data = supabase.table('propiedades').select('cliente_id').eq('id', servicio.propiedad_id).execute()
            if len(prop_data.data) > 0:
                cl_id = prop_data.data[0]['cliente_id']
                supabase.table('clientes').update({'estado': 'Cliente'}).eq('id', cl_id).execute()
        except Exception:
            pass

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
        return {"mensaje": "Actualizado", "datos": respuesta.data[0]}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.delete("/servicios/{servicio_id}")
async def eliminar_servicio(servicio_id: str):
    try:
        supabase.table('servicios_aplicacion').delete().eq('id', servicio_id).execute()
        return {"mensaje": "Eliminado exitosamente"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- RUTA DEL DASHBOARD (CON PARACAÍDAS CONTRA DATOS HUÉRFANOS) ---
@app.get("/api/dashboard/{mes_anio}")
async def obtener_dashboard(mes_anio: str):
    try:
        respuesta_metas = supabase.table('metas_mensuales').select('*').eq('mes_anio', mes_anio).execute()
        if len(respuesta_metas.data) == 0:
            metas = { "meta_ventas": 0, "meta_servicios": 0, "meta_clientes": 0, "meta_prospectos": 0, "meta_prospectos_visitas": 0 }
        else:
            metas = respuesta_metas.data[0]

        mes, anio = mes_anio.split("-")
        filtro_fecha = f"{anio}-{mes}" 
        
        # 1. Ventas e ingresos del mes (Con protección)
        respuesta_servicios = supabase.table('servicios_aplicacion').select('costo_servicio', 'fecha_aplicacion', 'propiedad_id').execute()
        servicios_mes = [s for s in respuesta_servicios.data if s.get('fecha_aplicacion') and s.get('fecha_aplicacion', '').startswith(filtro_fecha)]
        ventas_reales = sum(float(s.get('costo_servicio') or 0) for s in servicios_mes)
        servicios_reales = len(servicios_mes)
        
        # 2. Prospectos alcanzados
        respuesta_clientes = supabase.table('clientes').select('id', 'created_at').execute()
        prospectos_mes = [c for c in respuesta_clientes.data if c.get('created_at') and c.get('created_at', '').startswith(filtro_fecha)]
        prospectos_reales = len(prospectos_mes)

        # 3. Nuevos Clientes (A prueba de fallos: ignora si la parcela o el cliente ya no existen)
        respuesta_propiedades = supabase.table('propiedades').select('id', 'cliente_id').execute()
        mapa_propiedades = {p['id']: p.get('cliente_id') for p in respuesta_propiedades.data if p.get('cliente_id')}
        
        clientes_activos_mes = set()
        for s in servicios_mes:
            p_id = s.get('propiedad_id')
            if p_id and p_id in mapa_propiedades:
                clientes_activos_mes.add(mapa_propiedades[p_id])
        clientes_reales = len(clientes_activos_mes)

        return {
            "metas": metas, 
            "reales": {
                "ventas": ventas_reales, 
                "servicios": servicios_reales, 
                "clientes": clientes_reales, 
                "prospectos": prospectos_reales,
                "visitas": 0
            }
        }
    except Exception as e:
        print(f"Error crítico en Dashboard: {str(e)}") 
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/metas/")
async def guardar_metas(metas: MetasMensuales):
    try:
        existe = supabase.table('metas_mensuales').select('id').eq('mes_anio', metas.mes_anio).execute()
        if len(existe.data) > 0:
            respuesta = supabase.table('metas_mensuales').update(metas.dict()).eq('mes_anio', metas.mes_anio).execute()
        else:
            respuesta = supabase.table('metas_mensuales').insert(metas.dict()).execute()
        return {"mensaje": "Metas guardadas correctamente"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))