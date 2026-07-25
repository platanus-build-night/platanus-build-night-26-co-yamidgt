"""
RESPIRA - Red Estrategica de Preactivacion, Inteligencia y Respuesta Agil
Backend FastAPI + Socket.IO

Simula un sistema de despacho de ambulancias para emergencias "no-SOAT"
(pacientes sin cobertura de accidente de transito), con incentivos monetarios
para prestadores privados y un sistema de puntaje/ranking que castiga los
rechazos de servicio.

Ejecutar con:
    uvicorn backend:app --reload --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import math
import os
import random
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types as genai_types
import socketio
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuracion Socket.IO + FastAPI
# ---------------------------------------------------------------------------

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
fastapi_app = FastAPI(title="RESPIRA API")

fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Los archivos del dashboard y de la app ciudadana se sirven como estaticos
fastapi_app.mount("/static", StaticFiles(directory="static"), name="static")

# Carpeta donde se guardan las fotos/videos de evidencia subidos por el ciudadano.
# Al estar dentro de /static quedan servidos automaticamente en /static/evidencias/...
EVIDENCIAS_DIR = Path("static/evidencias")
EVIDENCIAS_DIR.mkdir(parents=True, exist_ok=True)
EXTENSIONES_PERMITIDAS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".mp4", ".mov", ".webm"}


@fastapi_app.get("/")
async def root():
    # Atajo para no tener que recordar la ruta completa en la demo
    return RedirectResponse(url="/static/ciudadano.html")


# ---------------------------------------------------------------------------
# Constantes de negocio
# ---------------------------------------------------------------------------

VELOCIDAD_PROMEDIO_KMH = 30.0
BONO_BASE_NO_SOAT = 50_000
BONO_RAPIDEZ = 30_000  # se otorga si la ambulancia llega en menos de 10 min
UMBRAL_RAPIDEZ_MIN = 10.0

PENALIZACION_RECHAZO = 5
PENALIZACION_RECHAZOS_CONSECUTIVOS = 15
UMBRAL_RECHAZOS_CONSECUTIVOS = 3

RADIO_LLEGADA_KM = 0.05  # se considera "llegada" cuando esta a <50m del evento
INTERVALO_MOVIMIENTO_SEG = 5
INTERVALO_NUEVO_EVENTO_SEG = 30
SEGUNDOS_COOLDOWN_RECHAZO = 8

# ---------------------------------------------------------------------------
# Triage de gravedad con IA (Google Gemini + vision)
# ---------------------------------------------------------------------------

IA_MODELO = "gemini-flash-latest"
MEDIA_TYPES_SOPORTADOS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

IA_ESQUEMA_TRIAGE = {
    "type": "object",
    "properties": {
        "puntaje_urgencia": {
            "type": "integer",
            "description": "0 a 100, donde 100 es la maxima urgencia medica posible",
        },
        "categoria": {"type": "string", "enum": ["urgente", "medio", "bajo"]},
        "razon": {"type": "string", "description": "explicacion breve, 1-2 frases en espanol"},
    },
    "required": ["puntaje_urgencia", "categoria", "razon"],
}

IA_PROMPT_TRIAGE = (
    "Eres un asistente de triage para el sistema de despacho de ambulancias RESPIRA. "
    "Analiza esta foto que un ciudadano adjunto a su reporte de emergencia medica (caso no-SOAT). "
    "Evalua que tan urgente parece la situacion basandote UNICAMENTE en evidencia visual "
    "(heridas, sangrado, posicion corporal, contexto del lugar, señales de gravedad). "
    "Esto es una sugerencia de apoyo para priorizar el despacho, NO un diagnostico medico "
    "y no reemplaza el criterio del personal de salud. "
    "Devuelve un puntaje de 0 a 100 (100 = maxima urgencia) y clasifica en 'urgente' (puntaje >= 70), "
    "'medio' (40 a 69) o 'bajo' (menor a 40)."
)

# Zonas reales aproximadas de Bogota usadas para distribuir ambulancias
# y para la heuristica de zonas calientes / cobertura
ZONAS = {
    "Chapinero": {"lat": 4.64, "lng": -74.06, "tipo": "residencial"},
    "Kennedy": {"lat": 4.61, "lng": -74.14, "tipo": "residencial"},
    "Suba": {"lat": 4.74, "lng": -74.08, "tipo": "residencial"},
    "Centro": {"lat": 4.61, "lng": -74.07, "tipo": "mixta"},
    "Usaquen": {"lat": 4.70, "lng": -74.03, "tipo": "residencial"},
}

# Parques usados en la heuristica de fin de semana
PARQUES = {
    "Parque Simon Bolivar": {"lat": 4.6584, "lng": -74.0937},
    "Parque El Virrey": {"lat": 4.6685, "lng": -74.0554},
    "Parque Nacional": {"lat": 4.6178, "lng": -74.0655},
    "Parque de la 93": {"lat": 4.6765, "lng": -74.0498},
    "Parque Timiza": {"lat": 4.6318, "lng": -74.1553},
}

NOMBRES_PRESTADORES = [
    "Ambulancias San Rafael",
    "Cruz Vida Rescate",
    "MedRescate Bogota",
    "Vitalis Ambulancias",
    "Grupo SOS Emergencias",
]

TIPOS_EVENTO = ["no-SOAT"]

# ---------------------------------------------------------------------------
# Estado en memoria (suficiente para una demo de hackathon, un solo proceso)
# ---------------------------------------------------------------------------

ambulancias: list[dict] = []
eventos: list[dict] = []
lock = asyncio.Lock()


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    """Distancia en linea recta entre dos coordenadas, en kilometros."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def eta_minutos(distancia_km: float) -> float:
    return (distancia_km / VELOCIDAD_PROMEDIO_KMH) * 60


def calcular_incentivo(distancia_km: float) -> dict:
    """Bono base por despacho no-SOAT + bono extra si la ambulancia
    alcanza a llegar en menos de 10 minutos (segun distancia simulada)."""
    eta = eta_minutos(distancia_km)
    bono_rapidez = BONO_RAPIDEZ if eta < UMBRAL_RAPIDEZ_MIN else 0
    return {
        "base": BONO_BASE_NO_SOAT,
        "bono_rapidez": bono_rapidez,
        "total": BONO_BASE_NO_SOAT + bono_rapidez,
        "eta_min": round(eta, 1),
    }


def jitter(valor: float, rango: float = 0.01) -> float:
    return valor + random.uniform(-rango, rango)


def nueva_ambulancia(idx: int, zona_nombre: str) -> dict:
    zona = ZONAS[zona_nombre]
    return {
        "id": f"amb-{idx:02d}",
        "nombre_prestador": NOMBRES_PRESTADORES[idx % len(NOMBRES_PRESTADORES)],
        "lat": jitter(zona["lat"]),
        "lng": jitter(zona["lng"]),
        "zona_base": zona_nombre,
        "estado": "disponible",  # disponible | ocupada | rechazo
        "puntaje": 100,
        "eventos_atendidos_hoy": 0,
        "rechazos_consecutivos": 0,
        "evento_asignado": None,
    }


def nuevo_evento(lat: float, lng: float, tipo: str = "no-SOAT", video_url: Optional[str] = None) -> dict:
    return {
        "id": str(uuid.uuid4())[:8],
        "lat": lat,
        "lng": lng,
        "tipo": tipo,
        "video_url": video_url,
        "timestamp": datetime.now().isoformat(),
        "estado": "pendiente",  # pendiente | asignado | en_camino | completado | cancelado
        "ambulancia_id": None,
        "incentivo_ofrecido": None,
        "eta_min": None,
        "tiempo_respuesta_min": None,
        "urgencia_ia": None,  # {"estado": "analizando"|"listo"|"error", "puntaje_urgencia", "categoria", "razon"}
    }


def inicializar_estado():
    idx = 0
    for zona_nombre in ZONAS:
        for _ in range(3):  # 5 zonas x 3 ambulancias = 15
            ambulancias.append(nueva_ambulancia(idx, zona_nombre))
            idx += 1

    # 3 eventos activos iniciales, repartidos en zonas distintas
    zonas_iniciales = random.sample(list(ZONAS.values()), 3)
    for z in zonas_iniciales:
        eventos.append(nuevo_evento(jitter(z["lat"]), jitter(z["lng"])))


inicializar_estado()


def ambulancia_por_id(amb_id: str) -> Optional[dict]:
    return next((a for a in ambulancias if a["id"] == amb_id), None)


def evento_por_id(evt_id: str) -> Optional[dict]:
    return next((e for e in eventos if e["id"] == evt_id), None)


def color_por_puntaje(puntaje: int) -> str:
    if puntaje > 80:
        return "verde"
    if puntaje >= 60:
        return "amarillo"
    return "rojo"


async def analizar_gravedad_ia(evento_id: str, ruta_archivo: Path, media_type: str):
    """Envia la foto de evidencia a Gemini para estimar que tan urgente es el
    caso (0-100 + categoria), y actualiza el evento cuando termina. Corre en
    background para no bloquear la respuesta de /evidencia; si no hay
    GEMINI_API_KEY configurada, se omite silenciosamente (la evidencia
    igual queda guardada y visible)."""
    if not os.environ.get("GEMINI_API_KEY"):
        print("[ia] GEMINI_API_KEY no configurada, se omite el analisis de gravedad")
        async with lock:
            evt = evento_por_id(evento_id)
            if evt:
                evt["urgencia_ia"] = None  # sin key configurada: no se queda "analizando" para siempre
        if evt:
            await sio.emit("evento_actualizado", evt)
        return

    try:
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        response = await client.aio.models.generate_content(
            model=IA_MODELO,
            contents=[
                genai_types.Part.from_bytes(data=ruta_archivo.read_bytes(), mime_type=media_type),
                IA_PROMPT_TRIAGE,
            ],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=IA_ESQUEMA_TRIAGE,
            ),
        )
        data = json.loads(response.text)
        resultado = {
            "estado": "listo",
            "puntaje_urgencia": data["puntaje_urgencia"],
            "categoria": data["categoria"],
            "razon": data["razon"],
        }
    except Exception as e:
        print(f"[ia] error analizando evidencia de {evento_id}: {e}")
        resultado = {"estado": "error", "detalle": "No se pudo completar el analisis"}

    async with lock:
        evt = evento_por_id(evento_id)
        if evt:
            evt["urgencia_ia"] = resultado

    if evt:
        await sio.emit("evento_actualizado", evt)


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------

class EmergenciaIn(BaseModel):
    lat: float
    lng: float
    tipo: str = "no-SOAT"
    video_url: Optional[str] = None


class AsignarIn(BaseModel):
    evento_id: str
    ambulancia_id: str
    incentivo_ofrecido: Optional[int] = None


class RechazarIn(BaseModel):
    evento_id: str
    ambulancia_id: str


# ---------------------------------------------------------------------------
# Endpoints REST
# ---------------------------------------------------------------------------

@fastapi_app.post("/api/emergencia")
async def crear_emergencia(data: EmergenciaIn):
    """El ciudadano reporta una emergencia no-SOAT. Se crea el evento,
    queda 'pendiente' y se notifica en tiempo real al CRUE (dashboard)."""
    evt = nuevo_evento(data.lat, data.lng, data.tipo, data.video_url)
    async with lock:
        eventos.append(evt)
    await sio.emit("nuevo_evento", evt)
    return {"id_evento": evt["id"], "evento": evt}


@fastapi_app.post("/api/eventos/{evento_id}/evidencia")
async def subir_evidencia(evento_id: str, archivo: UploadFile = File(...)):
    """El ciudadano sube una foto/video de evidencia para su emergencia.
    Se guarda en disco y queda visible tanto para el ciudadano (confirmacion)
    como para el despachador del CRUE (para evaluar gravedad antes de asignar)."""
    evt = evento_por_id(evento_id)
    if not evt:
        raise HTTPException(404, "Evento no encontrado")

    content_type = archivo.content_type or ""
    if not (content_type.startswith("image/") or content_type.startswith("video/")):
        raise HTTPException(400, "Solo se permiten imagenes o videos")

    ext = Path(archivo.filename or "").suffix.lower()
    if ext not in EXTENSIONES_PERMITIDAS:
        ext = ".jpg" if content_type.startswith("image/") else ".mp4"

    nombre_archivo = f"{evento_id}{ext}"
    destino = EVIDENCIAS_DIR / nombre_archivo
    with destino.open("wb") as f:
        shutil.copyfileobj(archivo.file, f)

    media_type_ia = MEDIA_TYPES_SOPORTADOS.get(ext)

    async with lock:
        evt["video_url"] = f"/static/evidencias/{nombre_archivo}"
        if media_type_ia:
            evt["urgencia_ia"] = {"estado": "analizando"}

    await sio.emit("evento_actualizado", evt)

    if media_type_ia:
        asyncio.create_task(analizar_gravedad_ia(evento_id, destino, media_type_ia))

    return {"ok": True, "url": evt["video_url"]}


@fastapi_app.get("/api/ambulancias")
async def listar_ambulancias():
    return ambulancias


@fastapi_app.get("/api/ambulancias_cercanas/{evento_id}")
async def ambulancias_cercanas(evento_id: str):
    """Usado por el modal de asignacion del dashboard: ambulancias
    disponibles ordenadas por distancia (y puntaje como desempate),
    junto con el incentivo sugerido para cada una."""
    evt = evento_por_id(evento_id)
    if not evt:
        raise HTTPException(404, "Evento no encontrado")

    candidatas = []
    for amb in ambulancias:
        if amb["estado"] != "disponible":
            continue
        dist = haversine_km(amb["lat"], amb["lng"], evt["lat"], evt["lng"])
        candidatas.append({
            **amb,
            "distancia_km": round(dist, 2),
            "incentivo_sugerido": calcular_incentivo(dist),
        })

    candidatas.sort(key=lambda a: (a["distancia_km"], -a["puntaje"]))
    return candidatas


@fastapi_app.get("/api/eventos")
async def listar_eventos():
    """Eventos activos (no completados/cancelados) con tiempo de espera."""
    activos = []
    ahora = datetime.now()
    for e in eventos:
        if e["estado"] in ("completado", "cancelado"):
            continue
        ts = datetime.fromisoformat(e["timestamp"])
        minutos_espera = round((ahora - ts).total_seconds() / 60, 1)
        activos.append({**e, "minutos_espera": minutos_espera})
    activos.sort(key=lambda e: e["timestamp"])
    return activos


@fastapi_app.get("/api/eventos/{evento_id}")
async def obtener_evento(evento_id: str):
    """Detalle de un evento + info de la ambulancia asignada (si existe).
    Usado por la app del ciudadano para hacer polling de su seguimiento."""
    evt = evento_por_id(evento_id)
    if not evt:
        raise HTTPException(404, "Evento no encontrado")
    amb = ambulancia_por_id(evt["ambulancia_id"]) if evt["ambulancia_id"] else None
    return {**evt, "ambulancia": amb}


@fastapi_app.post("/api/asignar")
async def asignar(data: AsignarIn):
    """El despachador del CRUE asigna una ambulancia a un evento.
    Calcula el incentivo (si no vino explicito) y arranca el desplazamiento
    simulado hacia el punto del evento."""
    async with lock:
        evt = evento_por_id(data.evento_id)
        amb = ambulancia_por_id(data.ambulancia_id)
        if not evt or not amb:
            raise HTTPException(404, "Evento o ambulancia no encontrados")
        if amb["estado"] != "disponible":
            raise HTTPException(400, "La ambulancia no esta disponible")

        dist = haversine_km(amb["lat"], amb["lng"], evt["lat"], evt["lng"])
        incentivo = data.incentivo_ofrecido or calcular_incentivo(dist)["total"]

        amb["estado"] = "ocupada"
        amb["evento_asignado"] = evt["id"]
        amb["rechazos_consecutivos"] = 0

        evt["estado"] = "asignado"
        evt["ambulancia_id"] = amb["id"]
        evt["incentivo_ofrecido"] = incentivo
        evt["eta_min"] = round(eta_minutos(dist), 1)

    await sio.emit("ambulancia_actualizada", amb)
    await sio.emit("evento_actualizado", evt)
    return {"ok": True, "ambulancia": amb, "evento": evt}


@fastapi_app.post("/api/rechazar")
async def rechazar(data: RechazarIn):
    """Una ambulancia rechaza el servicio ofrecido. Se penaliza su puntaje
    y, si acumula 3 rechazos consecutivos, recibe una penalizacion extra.
    El evento vuelve a quedar pendiente para ser reasignado."""
    async with lock:
        evt = evento_por_id(data.evento_id)
        amb = ambulancia_por_id(data.ambulancia_id)
        if not evt or not amb:
            raise HTTPException(404, "Evento o ambulancia no encontrados")

        amb["estado"] = "rechazo"
        amb["puntaje"] = max(0, amb["puntaje"] - PENALIZACION_RECHAZO)
        amb["rechazos_consecutivos"] += 1

        if amb["rechazos_consecutivos"] >= UMBRAL_RECHAZOS_CONSECUTIVOS:
            amb["puntaje"] = max(0, amb["puntaje"] - PENALIZACION_RECHAZOS_CONSECUTIVOS)
            amb["rechazos_consecutivos"] = 0

        if evt["estado"] not in ("completado", "cancelado"):
            evt["estado"] = "pendiente"
            evt["ambulancia_id"] = None
            evt["incentivo_ofrecido"] = None

    await sio.emit("ambulancia_actualizada", amb)
    await sio.emit("evento_actualizado", evt)
    await sio.emit("ranking_actualizado", await calcular_ranking())

    # La ambulancia vuelve a estar disponible tras un breve enfriamiento,
    # simulando el tiempo que tarda el CRUE en reintentar con ella.
    asyncio.create_task(_cooldown_rechazo(amb["id"]))

    return {"ok": True, "ambulancia": amb}


async def _cooldown_rechazo(amb_id: str):
    await asyncio.sleep(SEGUNDOS_COOLDOWN_RECHAZO)
    async with lock:
        amb = ambulancia_por_id(amb_id)
        if amb and amb["estado"] == "rechazo":
            amb["estado"] = "disponible"
    if amb:
        await sio.emit("ambulancia_actualizada", amb)


async def calcular_ranking():
    ranking = sorted(ambulancias, key=lambda a: -a["puntaje"])
    return [
        {
            "posicion": i + 1,
            "id": a["id"],
            "nombre_prestador": a["nombre_prestador"],
            "puntaje": a["puntaje"],
            "eventos_atendidos_hoy": a["eventos_atendidos_hoy"],
            "rechazos_consecutivos": a["rechazos_consecutivos"],
            "color": color_por_puntaje(a["puntaje"]),
        }
        for i, a in enumerate(ranking)
    ]


@fastapi_app.get("/api/ranking")
async def ranking():
    return await calcular_ranking()


@fastapi_app.get("/api/predict")
async def predict():
    """Heuristica simple de 'zonas calientes' segun la hora del dia:
    - Horas pico (6-8am o 4-6pm): prioriza zonas residenciales.
    - Fin de semana: prioriza parques (aglomeraciones, deporte).
    - Resto: mezcla ponderada por baja cobertura actual de ambulancias.
    """
    ahora = datetime.now()
    hora = ahora.hour
    es_fin_de_semana = ahora.weekday() >= 5
    es_hora_pico = (6 <= hora < 8) or (16 <= hora < 18)

    zonas_calientes = []

    if es_hora_pico:
        candidatos = [(n, z["lat"], z["lng"]) for n, z in ZONAS.items() if z["tipo"] == "residencial"]
    elif es_fin_de_semana:
        candidatos = [(n, p["lat"], p["lng"]) for n, p in PARQUES.items()]
    else:
        candidatos = [(n, z["lat"], z["lng"]) for n, z in ZONAS.items()]

    # Si hay menos de 5 candidatos naturales, completa con el resto de zonas
    if len(candidatos) < 5:
        extra = [(n, z["lat"], z["lng"]) for n, z in ZONAS.items()
                 if n not in [c[0] for c in candidatos]]
        candidatos += extra

    candidatos = candidatos[:5]

    for nombre, lat, lng in candidatos:
        disponibles_cerca = sum(
            1 for a in ambulancias
            if a["estado"] == "disponible" and haversine_km(a["lat"], a["lng"], lat, lng) < 3.0
        )
        # A menor cobertura, mayor intensidad de la zona caliente
        intensidad = round(max(0.2, min(1.0, 1.0 - (disponibles_cerca / 5))), 2)
        zonas_calientes.append({
            "nombre": nombre,
            "lat": jitter(lat, 0.005),
            "lng": jitter(lng, 0.005),
            "intensidad": intensidad,
            "ambulancias_cerca": disponibles_cerca,
        })

    zonas_calientes.sort(key=lambda z: -z["intensidad"])
    return zonas_calientes


@fastapi_app.get("/api/metricas")
async def metricas():
    completados = [e for e in eventos if e["estado"] == "completado" and e["tiempo_respuesta_min"] is not None]
    tiempo_promedio = (
        round(sum(e["tiempo_respuesta_min"] for e in completados) / len(completados), 1)
        if completados else 0.0
    )

    total_finalizados_o_activos = [e for e in eventos if e["estado"] != "cancelado"]
    exitosos = [e for e in eventos if e["estado"] in ("en_camino", "completado", "asignado")]
    porcentaje_asignacion = (
        round(len(exitosos) / len(total_finalizados_o_activos) * 100, 1)
        if total_finalizados_o_activos else 0.0
    )

    disponibles_ahora = sum(1 for a in ambulancias if a["estado"] == "disponible")

    return {
        "tiempo_promedio_respuesta_hoy": tiempo_promedio,
        "porcentaje_asignacion_exitosa": porcentaje_asignacion,
        "ambulancias_disponibles_ahora": disponibles_ahora,
    }


# ---------------------------------------------------------------------------
# Eventos Socket.IO
# ---------------------------------------------------------------------------

@sio.event
async def connect(sid, environ):
    print(f"[socket] cliente conectado: {sid}")


@sio.event
async def disconnect(sid):
    print(f"[socket] cliente desconectado: {sid}")


# ---------------------------------------------------------------------------
# Simulacion en background
# ---------------------------------------------------------------------------

async def mover_ambulancias_loop():
    """Cada INTERVALO_MOVIMIENTO_SEG segundos, avanza las ambulancias
    ocupadas un paso hacia el evento asignado, a velocidad promedio de
    30km/h. Al llegar, marca el evento como completado y libera la
    ambulancia (sumando puntaje y su contador de eventos atendidos)."""
    while True:
        await asyncio.sleep(INTERVALO_MOVIMIENTO_SEG)
        async with lock:
            for amb in ambulancias:
                if amb["estado"] != "ocupada" or not amb["evento_asignado"]:
                    continue
                evt = evento_por_id(amb["evento_asignado"])
                if not evt:
                    continue

                if evt["estado"] == "asignado":
                    # La unidad ya arranco: pasa de "asignado" a "en_camino"
                    evt["estado"] = "en_camino"

                dist_km = haversine_km(amb["lat"], amb["lng"], evt["lat"], evt["lng"])
                paso_km = VELOCIDAD_PROMEDIO_KMH * (INTERVALO_MOVIMIENTO_SEG / 3600)

                if dist_km <= max(paso_km, RADIO_LLEGADA_KM):
                    # Llego al sitio del evento
                    amb["lat"], amb["lng"] = evt["lat"], evt["lng"]
                    amb["estado"] = "disponible"
                    amb["evento_asignado"] = None
                    amb["eventos_atendidos_hoy"] += 1
                    amb["puntaje"] = min(100, amb["puntaje"] + 2)

                    ts = datetime.fromisoformat(evt["timestamp"])
                    evt["tiempo_respuesta_min"] = round((datetime.now() - ts).total_seconds() / 60, 1)
                    evt["estado"] = "completado"
                    evt["eta_min"] = 0

                    await sio.emit("ambulancia_actualizada", amb)
                    await sio.emit("evento_actualizado", evt)
                    await sio.emit("ranking_actualizado", await calcular_ranking())
                else:
                    frac = paso_km / dist_km
                    amb["lat"] += (evt["lat"] - amb["lat"]) * frac
                    amb["lng"] += (evt["lng"] - amb["lng"]) * frac
                    nueva_dist = haversine_km(amb["lat"], amb["lng"], evt["lat"], evt["lng"])
                    evt["eta_min"] = round(eta_minutos(nueva_dist), 1)

                    await sio.emit("ambulancia_actualizada", amb)
                    await sio.emit("evento_actualizado", evt)


async def generar_eventos_loop():
    """Cada INTERVALO_NUEVO_EVENTO_SEG segundos, genera un evento nuevo
    en la zona con menor cobertura de ambulancias disponibles, para
    demostrar visualmente la necesidad del sistema de despacho."""
    while True:
        await asyncio.sleep(INTERVALO_NUEVO_EVENTO_SEG)
        async with lock:
            peor_zona = min(
                ZONAS.items(),
                key=lambda item: sum(
                    1 for a in ambulancias
                    if a["estado"] == "disponible"
                    and haversine_km(a["lat"], a["lng"], item[1]["lat"], item[1]["lng"]) < 3.0
                ),
            )
            nombre_zona, coords = peor_zona
            evt = nuevo_evento(jitter(coords["lat"]), jitter(coords["lng"]))
            eventos.append(evt)

        await sio.emit("nuevo_evento", evt)
        print(f"[sim] nuevo evento generado en zona de baja cobertura: {nombre_zona}")


@fastapi_app.on_event("startup")
async def iniciar_simulacion():
    asyncio.create_task(mover_ambulancias_loop())
    asyncio.create_task(generar_eventos_loop())


# ---------------------------------------------------------------------------
# App combinada: Socket.IO envuelve a FastAPI para compartir el mismo puerto.
# "app" es lo que uvicorn debe servir (uvicorn backend:app) para que tanto
# las rutas REST como las de socket.io ("/socket.io/...") respondan.
# ---------------------------------------------------------------------------
app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)
