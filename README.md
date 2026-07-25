# RESPIRA

![project-logo](project-logo.png)

Sistema de despacho inteligente de ambulancias para emergencias medicas.

Conecta a ciudadanos que reportan emergencias con prestadores privados de transporte medico, asignando unidades disponibles segun cercania y puntaje de confiabilidad, con incentivos monetarios por despacho y rapidez.

## Despliegue

- Ciudadano: https://respira-kxro.onrender.com/static/ciudadano.html
- Dashboard CRUE: https://respira-kxro.onrender.com/static/dashboard.html

## Caracteristicas

- **Panel CRUE en tiempo real** — mapa con eventos activos, mapa de calor de zonas criticas y seguimiento de ambulancias
- **Triage asistido por IA** — analisis de gravedad con Google Gemini basado en evidencia visual
- **Incentivos automaticos** — bono base + bono de rapidez para despachos agiles
- **Sistema de ranking** — puntaje y penalizacion por rechazos consecutivos
- **Evidencia multimedia** — foto/video adjunto a cada emergencia, visible para el despachador
- **Seguimiento ciudadano** — app para reportar emergencias y rastrear asignacion en tiempo real

## Stack

- FastAPI + Socket.IO (backend)
- HTML/CSS/JS vanilla (frontend)
- Leaflet (mapas)
- Google Gemini (triage IA)
