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

# --- RUTAS DE SERVICIOS ---
@app.post("/servicios/")
async def registrar_servicio(servicio: ServicioNuevo):
    try:
        respuesta = supabase.table('servicios_aplicacion').insert(servicio.dict()).execute()
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

# --- RUTA DEL DASHBOARD (METAS VS REALIDAD) ---
@app.get("/api/dashboard/{mes_anio}")
async def obtener_dashboard(mes_anio: str):
    try:
        # 1. Buscar la meta
        respuesta_metas = supabase.table('metas_mensuales').select('*').eq('mes_anio', mes_anio).execute()
        
        if len(respuesta_metas.data) == 0:
            metas = {
                "meta_ventas": 0, "meta_servicios": 0, 
                "meta_clientes": 0, "meta_prospectos": 0, "meta_visitas": 0
            }
        else:
            metas = respuesta_metas.data[0]

        # 2. Calcular la realidad
        mes, anio = mes_anio.split("-")
        filtro_fecha = f"{anio}-{mes}" 

        respuesta_servicios = supabase.table('servicios_aplicacion').select('costo_servicio', 'fecha_aplicacion').execute()
        servicios_mes = [s for s in respuesta_servicios.data if s.get('fecha_aplicacion', '').startswith(filtro_fecha)]
        
        ventas_reales = sum(float(s.get('costo_servicio', 0)) for s in servicios_mes)
        servicios_reales = len(servicios_mes)

        respuesta_clientes = supabase.table('clientes').select('id').execute()
        clientes_reales = len(respuesta_clientes.data)

        # 3. Devolver empaquetado
        return {
            "metas": metas,
            "reales": {
                "ventas": ventas_reales,
                "servicios": servicios_reales,
                "clientes": clientes_reales
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))