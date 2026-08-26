"""Translation catalogue for server-generated text.

Much of what the operator reads — evidence labels, detection rules, proposed
solutions, autonomy decisions, activity events — is produced in Python, so the
UI cannot translate it on its own. Strings live here as keys with named
placeholders; `t()` resolves and formats them.

Adding a language means adding one dictionary. A missing key falls back to
English rather than showing a raw key to the user.
"""

from typing import Any, Dict

DEFAULT_LANG = "en"
SUPPORTED = ("en", "es")

CATALOG: Dict[str, Dict[str, str]] = {
    "en": {
        # --- Evidence labels ---------------------------------------------
        "ev.allocated_cpu": "Allocated CPU",
        "ev.allocated_memory": "Allocated memory",
        "ev.billing_model": "Billing model",
        "ev.cpu_peak": "CPU peak ({hours}h)",
        "ev.memory_peak": "Memory peak ({hours}h)",
        "ev.monthly_cost": "Estimated monthly cost",
        "ev.waste": "Estimated waste",
        "ev.type": "Type",
        "ev.size": "Size",
        "ev.zone": "Zone",
        "ev.attached_to": "Attached to",
        "ev.address": "Address",
        "ev.region": "Region",
        "ev.status": "Status",
        "ev.repository": "Repository",
        "ev.tags": "Tags",
        "ev.created": "Created",
        "ev.cost": "Monthly cost",
        # --- Evidence values ---------------------------------------------
        "val.billed_always": "{count} instance(s) billed 24/7",
        "val.scale_to_zero": "scale-to-zero (billed per request)",
        "val.nothing": "nothing",
        "val.none": "none",
        "val.reserved_unused": "RESERVED, not in use",
        # --- Sources ------------------------------------------------------
        "src.cloud_run": "Cloud Run API",
        "src.monitoring": "Cloud Monitoring",
        "src.modelled": "Modelled",
        "src.cost_model": "Cost model",
        "src.compute": "Compute API",
        "src.pricing": "GCP pricing",
        "src.artifact": "Artifact Registry",
        # --- Rules --------------------------------------------------------
        "rule.idle_always_on.cond": "min_instances > 0 AND CPU peak < 10%",
        "rule.idle_always_on.obs": "min_instances = {min_instances}, CPU peak = {cpu}%",
        "rule.idle_always_on.why": (
            "{min_instances} instance(s) are billed around the clock regardless of "
            "traffic. With almost no CPU use, that capacity is paid for and unused — "
            "the most expensive form of idle on Cloud Run."
        ),
        "rule.idle_service.cond": "CPU peak < 10% AND memory peak < 20%",
        "rule.idle_service.obs": "CPU peak = {cpu}%, memory peak = {mem}%",
        "rule.idle_service.why": (
            "The service is provisioned for load it never receives. Its allocation "
            "can be reduced without affecting the traffic it actually serves."
        ),
        "rule.oversized.cond": "memory >= {threshold}Gi AND headroom > 50%",
        "rule.oversized.obs": "memory = {memory}, memory peak = {mem}%",
        "rule.oversized.why": (
            "Memory is allocated per instance for the whole lifetime of the revision, "
            "so the gap between the limit and real usage is billed continuously."
        ),
        "rule.orphan_disk.cond": "disk.users is empty",
        "rule.orphan_disk.obs": "no instance references this disk",
        "rule.orphan_disk.why": (
            "Persistent disks bill for provisioned capacity whether or not they are "
            "attached. An unattached disk is 100% waste — nothing can read it."
        ),
        "rule.unused_ip.cond": "address.status != IN_USE AND no users",
        "rule.unused_ip.obs": "reserved with nothing attached",
        "rule.unused_ip.why": (
            "Google charges for a static IP precisely when it is idle — an in-use "
            "external IP on a running instance is free, an unattached one is not."
        ),
        "rule.untagged_image.cond": "version has no related tags",
        "rule.untagged_image.obs": "no tag references this digest",
        "rule.untagged_image.why": (
            "Untagged image versions are usually superseded build layers. Nothing can "
            "deploy them by name, but they still occupy billed storage."
        ),
        # --- Verdicts -----------------------------------------------------
        "verdict.Healthy": "Healthy",
        "verdict.Tolerated": "Within threshold",
        "verdict.Idle": "Idle",
        "verdict.Oversized": "Oversized",
        "verdict.Orphaned": "Orphaned",
        "verdict.Unused": "Unused",
        "verdict.Untagged": "Untagged",
        # --- Diagnosis / solutions ---------------------------------------
        "diag.healthy": "Allocation is proportionate to observed usage. No action recommended.",
        "diag.tolerated": (
            "Utilization is low, but only ${waste}/mo is recoverable — below the "
            "${threshold}/mo action threshold. The resource is correctly sized for "
            "practical purposes and no change is proposed."
        ),
        "diag.orphan_disk": "Unattached persistent disk billing at full rate.",
        "diag.unused_ip": "Static IP reserved but attached to nothing.",
        "diag.untagged_image": "Untagged image version occupying registry storage.",
        "sol.apply": "Apply {changes}.",
        "sol.none": "No safe reduction available at current usage.",
        "sol.delete_disk": "Snapshot if the data matters, then delete {id}.",
        "sol.release_ip": "Release {id} if no DNS record still points at it.",
        "sol.delete_image": "Delete the untagged version.",
        "chg.memory": "memory {from_} → {to}",
        "chg.cpu": "CPU {from_} → {to}",
        "chg.min_instances": "min-instances {from_} → {to}",
        # --- Expected result ----------------------------------------------
        "result.resize": (
            "Estimated cost drops from ${before}/mo to about ${after}/mo, "
            "saving ${savings}/mo (${yearly}/year)."
        ),
        "result.remove": "Removes ${savings}/mo (${yearly}/year) of cost.",
        # --- Autonomy -----------------------------------------------------
        "auto.level.none": "None",
        "auto.level.1": "Level 1",
        "auto.level.2": "Level 2",
        "auto.dec.reported": "Reported only",
        "auto.dec.approval": "Requires human approval",
        "auto.dec.auto": "Executed autonomously",
        "auto.dec.noaction": "No action",
        "auto.why.below_threshold": (
            "${savings}/mo is below the ${threshold} action threshold. Acting would "
            "cost more attention than it saves."
        ),
        "auto.why.high_value": (
            "${savings}/mo exceeds the ${threshold} threshold. A change this material "
            "on a running service is validated by a person before it is applied."
        ),
        "auto.why.low_risk": (
            "${savings}/mo is a low-risk change under the ${threshold} threshold, so "
            "the agent applies it directly and records it in the memory bank."
        ),
        "auto.why.irreversible_disk": (
            "Disk deletion is irreversible and the data cannot be recovered afterwards, "
            "so it always needs a person — regardless of savings."
        ),
        "auto.why.irreversible_ip": (
            "Releasing the address is permanent — the same IP cannot be reclaimed. If "
            "DNS or an allow-list still references it, releasing breaks them."
        ),
        "auto.why.safe_reclaim": (
            "An untagged version cannot be referenced by a deployment, so removing it "
            "is a safe reclaim."
        ),
        "auto.why.nothing": "Nothing to fix.",
        # --- Confidence ----------------------------------------------------
        "conf.modelled": (
            "Utilization is modelled, not measured — Cloud Monitoring returned no data "
            "for this service. Treat the sizing as indicative only."
        ),
        "conf.sparse": (
            "Only {n} of 24 metric buckets are available. The service has not been "
            "observed long enough to establish its steady-state peak."
        ),
        "conf.partial": (
            "{n} of 24 metric buckets available. Enough to act on, but a longer window "
            "would tighten the estimate."
        ),
        "conf.full": "{n} metric buckets over {hours}h from Cloud Monitoring.",
        "conf.direct_read": "Attachment state is read directly from the Compute API; it is not inferred.",
        "conf.tags_read": "Tag state read from Artifact Registry.",
        "cap.reduction": (
            "Reduction capped at {factor}x per audit; a further step becomes available "
            "once the new shape has been observed."
        ),
        # --- Activity events ------------------------------------------------
        "ev.online": "Sentinel online",
        "ev.offline": "Sentinel shutting down",
        "ev.audit_started": "Audit {run_id} started — {count} anomalies in scope",
        "ev.audit_finished": "Audit {run_id} finished — {count} action(s) dispatched",
        "ev.audit_failed": "Audit {run_id} failed: {error}",
        "ev.remediation_applied": "{action} applied to {resource} (+${savings}/mo)",
        "ev.remediation_simulated": "{action} simulated on {resource} (+${savings}/mo)",
        "ev.approval_requested": "Approval requested: {action} on {resource}",
        "ev.human_decision": "Human {decision} '{action}' on {resource}",
        "ev.llm_unavailable": "Gemini unavailable ({error}) — completed the audit heuristically",
        "ev.memory_reset": "Memory bank reset",
        "radar.cost": "Cost",
        "radar.rightsizing": "Right-sizing",
        "radar.scaling": "Scaling",
        "radar.observability": "Observability",
        "radar.automation": "Automation",
        "radar.governance": "Governance",
        "usage.unattached": "unattached",
        "usage.not_in_use": "not in use",
        "usage.untagged": "untagged",
        "act.right_size": "Right-size allocation to match observed usage",
        "act.delete_disk": "Delete orphaned persistent disk",
        "act.release_ip": "Release unused static IP",
        "act.purge_image": "Purge untagged container image",
        "reason.autonomy2": (
            "Autonomy Level 2: a ${savings}/mo change on a production service requires "
            "human validation before execution."
        ),
        "reason.irreversible": (
            "Autonomy Level 2: this action is irreversible and always requires human "
            "validation, regardless of estimated savings."
        ),
        "ev.decision.approved": "approved",
        "ev.decision.rejected": "rejected",
    },
    "es": {
        # --- Etiquetas de evidencia ---------------------------------------
        "ev.allocated_cpu": "CPU asignada",
        "ev.allocated_memory": "Memoria asignada",
        "ev.billing_model": "Modelo de facturación",
        "ev.cpu_peak": "Pico de CPU ({hours}h)",
        "ev.memory_peak": "Pico de memoria ({hours}h)",
        "ev.monthly_cost": "Costo mensual estimado",
        "ev.waste": "Desperdicio estimado",
        "ev.type": "Tipo",
        "ev.size": "Tamaño",
        "ev.zone": "Zona",
        "ev.attached_to": "Adjunto a",
        "ev.address": "Dirección",
        "ev.region": "Región",
        "ev.status": "Estado",
        "ev.repository": "Repositorio",
        "ev.tags": "Etiquetas",
        "ev.created": "Creado",
        "ev.cost": "Costo mensual",
        # --- Valores -------------------------------------------------------
        "val.billed_always": "{count} instancia(s) facturadas 24/7",
        "val.scale_to_zero": "escala a cero (se factura por request)",
        "val.nothing": "nada",
        "val.none": "ninguna",
        "val.reserved_unused": "RESERVADA, sin usar",
        # --- Fuentes --------------------------------------------------------
        "src.cloud_run": "API de Cloud Run",
        "src.monitoring": "Cloud Monitoring",
        "src.modelled": "Modelado",
        "src.cost_model": "Modelo de costos",
        "src.compute": "API de Compute",
        "src.pricing": "Precios de GCP",
        "src.artifact": "Artifact Registry",
        # --- Reglas ---------------------------------------------------------
        "rule.idle_always_on.cond": "min_instances > 0 Y pico de CPU < 10%",
        "rule.idle_always_on.obs": "min_instances = {min_instances}, pico de CPU = {cpu}%",
        "rule.idle_always_on.why": (
            "{min_instances} instancia(s) se facturan las 24 horas sin importar el "
            "tráfico. Con casi nada de uso de CPU, esa capacidad se paga y no se usa: "
            "la forma más cara de estar ocioso en Cloud Run."
        ),
        "rule.idle_service.cond": "pico de CPU < 10% Y pico de memoria < 20%",
        "rule.idle_service.obs": "pico de CPU = {cpu}%, pico de memoria = {mem}%",
        "rule.idle_service.why": (
            "El servicio está aprovisionado para una carga que nunca recibe. Su "
            "asignación puede reducirse sin afectar el tráfico que realmente atiende."
        ),
        "rule.oversized.cond": "memoria >= {threshold}Gi Y holgura > 50%",
        "rule.oversized.obs": "memoria = {memory}, pico de memoria = {mem}%",
        "rule.oversized.why": (
            "La memoria se asigna por instancia durante toda la vida de la revisión, "
            "así que la brecha entre el límite y el uso real se factura de forma continua."
        ),
        "rule.orphan_disk.cond": "disk.users está vacío",
        "rule.orphan_disk.obs": "ninguna instancia referencia este disco",
        "rule.orphan_disk.why": (
            "Los discos persistentes facturan la capacidad aprovisionada estén o no "
            "adjuntos. Un disco sin adjuntar es 100% desperdicio: nada puede leerlo."
        ),
        "rule.unused_ip.cond": "address.status != IN_USE Y sin usuarios",
        "rule.unused_ip.obs": "reservada sin nada adjunto",
        "rule.unused_ip.why": (
            "Google cobra por una IP estática precisamente cuando está ociosa: una IP "
            "externa en uso sobre una instancia activa es gratis, una sin adjuntar no."
        ),
        "rule.untagged_image.cond": "la versión no tiene etiquetas asociadas",
        "rule.untagged_image.obs": "ninguna etiqueta referencia este digest",
        "rule.untagged_image.why": (
            "Las versiones de imagen sin etiquetar suelen ser capas de build "
            "reemplazadas. Nada puede desplegarlas por nombre, pero siguen ocupando "
            "almacenamiento facturado."
        ),
        # --- Veredictos ------------------------------------------------------
        "verdict.Healthy": "Saludable",
        "verdict.Tolerated": "Dentro del umbral",
        "verdict.Idle": "Ocioso",
        "verdict.Oversized": "Sobredimensionado",
        "verdict.Orphaned": "Huérfano",
        "verdict.Unused": "Sin usar",
        "verdict.Untagged": "Sin etiquetar",
        # --- Diagnóstico / soluciones ----------------------------------------
        "diag.healthy": "La asignación es proporcional al uso observado. No se recomienda ninguna acción.",
        "diag.tolerated": (
            "El uso es bajo, pero solo se pueden recuperar ${waste}/mes — por debajo "
            "del umbral de acción de ${threshold}/mes. El recurso está correctamente "
            "dimensionado a efectos prácticos y no se propone ningún cambio."
        ),
        "diag.orphan_disk": "Disco persistente sin adjuntar, facturando a tarifa completa.",
        "diag.unused_ip": "IP estática reservada pero sin nada adjunto.",
        "diag.untagged_image": "Versión de imagen sin etiquetar ocupando almacenamiento del registro.",
        "sol.apply": "Aplicar {changes}.",
        "sol.none": "No hay reducción segura disponible con el uso actual.",
        "sol.delete_disk": "Tomar snapshot si los datos importan, y luego eliminar {id}.",
        "sol.release_ip": "Liberar {id} si ningún registro DNS la sigue apuntando.",
        "sol.delete_image": "Eliminar la versión sin etiquetar.",
        "chg.memory": "memoria {from_} → {to}",
        "chg.cpu": "CPU {from_} → {to}",
        "chg.min_instances": "min-instances {from_} → {to}",
        # --- Resultado esperado ----------------------------------------------
        "result.resize": (
            "El costo estimado baja de ${before}/mes a unos ${after}/mes, "
            "ahorrando ${savings}/mes (${yearly}/año)."
        ),
        "result.remove": "Elimina ${savings}/mes (${yearly}/año) de costo.",
        # --- Autonomía ---------------------------------------------------------
        "auto.level.none": "Ninguno",
        "auto.level.1": "Nivel 1",
        "auto.level.2": "Nivel 2",
        "auto.dec.reported": "Solo reportado",
        "auto.dec.approval": "Requiere aprobación humana",
        "auto.dec.auto": "Ejecutado de forma autónoma",
        "auto.dec.noaction": "Sin acción",
        "auto.why.below_threshold": (
            "${savings}/mes está por debajo del umbral de acción de ${threshold}. "
            "Actuar costaría más atención de la que ahorra."
        ),
        "auto.why.high_value": (
            "${savings}/mes supera el umbral de ${threshold}. Un cambio de esta "
            "magnitud sobre un servicio en marcha lo valida una persona antes de aplicarse."
        ),
        "auto.why.low_risk": (
            "${savings}/mes es un cambio de bajo riesgo, por debajo del umbral de "
            "${threshold}, así que el agente lo aplica directamente y lo registra en "
            "el banco de memoria."
        ),
        "auto.why.irreversible_disk": (
            "Eliminar un disco es irreversible y los datos no se pueden recuperar "
            "después, así que siempre necesita una persona, sin importar el ahorro."
        ),
        "auto.why.irreversible_ip": (
            "Liberar la dirección es permanente: la misma IP no se puede reclamar. Si "
            "algún DNS o lista de permitidos la sigue referenciando, se rompen."
        ),
        "auto.why.safe_reclaim": (
            "Una versión sin etiquetar no puede ser referenciada por un despliegue, "
            "así que eliminarla es una recuperación segura."
        ),
        "auto.why.nothing": "No hay nada que corregir.",
        # --- Confianza -----------------------------------------------------------
        "conf.modelled": (
            "La utilización está modelada, no medida: Cloud Monitoring no devolvió "
            "datos para este servicio. Tomá el dimensionamiento solo como indicativo."
        ),
        "conf.sparse": (
            "Solo hay {n} de 24 buckets de métricas disponibles. El servicio no se "
            "observó el tiempo suficiente para establecer su pico en régimen."
        ),
        "conf.partial": (
            "{n} de 24 buckets de métricas disponibles. Suficiente para actuar, pero "
            "una ventana más larga afinaría la estimación."
        ),
        "conf.full": "{n} buckets de métricas sobre {hours}h de Cloud Monitoring.",
        "conf.direct_read": "El estado de adjunción se lee directamente de la API de Compute; no se infiere.",
        "conf.tags_read": "Estado de etiquetas leído de Artifact Registry.",
        "cap.reduction": (
            "Reducción limitada a {factor}x por auditoría; un paso adicional queda "
            "disponible una vez observada la nueva forma."
        ),
        # --- Eventos de actividad --------------------------------------------------
        "ev.online": "Sentinel en línea",
        "ev.offline": "Sentinel apagándose",
        "ev.audit_started": "Auditoría {run_id} iniciada — {count} anomalías en alcance",
        "ev.audit_finished": "Auditoría {run_id} finalizada — {count} acción(es) despachadas",
        "ev.audit_failed": "Auditoría {run_id} falló: {error}",
        "ev.remediation_applied": "{action} aplicado a {resource} (+${savings}/mes)",
        "ev.remediation_simulated": "{action} simulado en {resource} (+${savings}/mes)",
        "ev.approval_requested": "Aprobación solicitada: {action} en {resource}",
        "ev.human_decision": "Un humano {decision} '{action}' en {resource}",
        "ev.llm_unavailable": "Gemini no disponible ({error}) — auditoría completada de forma heurística",
        "ev.memory_reset": "Banco de memoria reiniciado",
        "radar.cost": "Costo",
        "radar.rightsizing": "Dimensionado",
        "radar.scaling": "Escalado",
        "radar.observability": "Observabilidad",
        "radar.automation": "Automatización",
        "radar.governance": "Gobernanza",
        "usage.unattached": "sin adjuntar",
        "usage.not_in_use": "sin uso",
        "usage.untagged": "sin etiquetar",
        "act.right_size": "Ajustar la asignación al uso observado",
        "act.delete_disk": "Eliminar disco persistente huérfano",
        "act.release_ip": "Liberar IP estática sin usar",
        "act.purge_image": "Purgar imagen de contenedor sin etiquetar",
        "reason.autonomy2": (
            "Autonomía Nivel 2: un cambio de ${savings}/mes sobre un servicio en "
            "producción requiere validación humana antes de ejecutarse."
        ),
        "reason.irreversible": (
            "Autonomía Nivel 2: esta acción es irreversible y siempre requiere "
            "validación humana, sin importar el ahorro estimado."
        ),
        "ev.decision.approved": "aprobó",
        "ev.decision.rejected": "rechazó",
    },
}


def normalise(lang: Any) -> str:
    """Accept 'es', 'es-AR', 'ES' — anything else falls back to English."""
    if not lang:
        return DEFAULT_LANG
    code = str(lang).split(",")[0].split("-")[0].strip().lower()
    return code if code in SUPPORTED else DEFAULT_LANG


def t(lang: str, key: str, **params: Any) -> str:
    """Resolve a key, formatting any placeholders.

    Falls back to English, then to the key itself, so a missing translation
    degrades to readable text instead of an exception.
    """
    lang = normalise(lang)
    template = CATALOG.get(lang, {}).get(key) or CATALOG[DEFAULT_LANG].get(key) or key
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return template
