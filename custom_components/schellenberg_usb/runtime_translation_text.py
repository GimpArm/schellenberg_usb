"""Translations for UI text that is computed in Python at runtime.

Home Assistant's own translation mechanism (strings.json / translations/*.json)
only covers text it resolves itself: step titles/descriptions, field labels,
menu options, and error/abort codes. Anything this integration builds itself
at runtime - a Developer Tools notice assembled from an f-string, the
copy-diagnostics text dump, a device's registered "model" string, a
HomeAssistantError/ServiceValidationError message - bypasses that mechanism
entirely and would otherwise stay hardcoded English no matter which language
Home Assistant is configured in.

This module is the hand-rolled equivalent for exactly that text, keyed by
hass.config.language with an English fallback. It is intentionally not
routed through homeassistant.helpers.translation.async_get_translations():
that helper's support for a custom component's own non-standard top-level
JSON categories could not be verified without a real Home Assistant
instance, so a small dependency-free dict lookup is used instead.

Two complementary lookups are provided:

- runtime_translation_text(hass, key, **kwargs): a parameterized template, for text that
  is assembled from an f-string (a notice, an error message, one line of the
  diagnostics dump).
- translate_phrase(hass, phrase)/yes_no(hass, value): a flat literal lookup
  for short, reusable English words/phrases that already exist as plain
  values elsewhere in the integration (cover.py's internal position/status
  bookkeeping, api.py's device_mode/identity_role values, "Yes"/"No"). Those
  values are deliberately left as plain English literals at the point they
  are produced - they are also used for internal comparisons/log messages -
  and are only translated here, at the moment they reach the UI.

Every translation below aims to read naturally in the target language
rather than mirroring the English wording word for word (a literal
translation of some earlier strings - e.g. the German "Endegrund" for
"end reason" - was confusing and has been corrected; the lesson is applied
throughout this module).
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

_RUNTIME_TRANSLATION_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "no_test_command": "No test command has been sent in this session yet.",
        "manually_entered": "manually entered",
        "not_discovered": "not discovered",
        "none_recorded": "None recorded",
        "none_word": "None",
        "never": "Never",
        "not_recorded": "Not recorded",
        "unknown_label": "Unknown",
        "device_fallback_name": "Device {device_id}",
        "device_generic": "Device",
        "device_model": "USB Stick",
        "device_model_motor": (
            "USB Stick Motor (command {command_device_id}/{command_enum}, "
            "primary status {primary_status_text}, "
            "secondary statuses {secondary_count})"
        ),
        "command_not_sent": "the command was not sent",
        "block_disconnected": "the serial stick is disconnected",
        "block_transport_unavailable": "the serial transport is unavailable",
        "block_transport_closing": "the serial transport is closing",
        "block_pairing_active": "pairing is currently active",
        "block_wrong_mode": (
            "the stick is in {mode} mode, but listening mode is needed"
        ),
        "block_busy_latched": ("the stick reported busy repeatedly and needs a reset"),
        "block_pending_retry": (
            "a transmission is still pending for payload {payload}"
        ),
        "notice_command_blocked": (
            '{command} command blocked: {reason}. Use "Reset stick", or '
            "reconnect the serial port, if this does not clear on its own."
        ),
        "notice_command_sent": "{command} command sent successfully.",
        "notice_command_failed": (
            "{command} command failed; check the integration logs."
        ),
        "notice_position_confirmed": (
            "Position manually confirmed at {position}%. No RF command was sent."
        ),
        "notice_position_sync_unregistered": (
            "Manual position sync failed because the live cover entity is not "
            "registered. Reload the integration and try again."
        ),
        "notice_teach_blocked": "Motor teach blocked: {reason}.",
        "notice_teach_test_sent": (
            "Teach, Open, and Stop were transmitted. The stick's "
            "acknowledgements only confirm radio transmission - please "
            "verify that the motor actually reacted."
        ),
        "notice_teach_test_failed": (
            "Teach/test transmission failed; check the integration logs."
        ),
        "test_motor_retry_notice": (
            "The last attempt didn't confirm the shutter moved - choose a "
            "direction to try again."
        ),
        "notice_raw_sent": (
            "Raw payload {payload} was written. The stick's acknowledgements "
            "do not confirm motor movement."
        ),
        "notice_reset_ready": (
            "Stick reset and serial reconnect completed; ready to transmit."
        ),
        "notice_reset_not_ready": (
            "Stick reset/reconnect did not become ready (connected={connected}, "
            "mode={mode}). Check the integration logs and the USB connection."
        ),
        "source_calibration": "automatically discovered during calibration",
        "source_remote_discovery": (
            "automatically discovered from the original remote"
        ),
        "source_unknown": "unknown / not yet discovered",
        "source_legacy": "legacy configuration / not verified",
        "position_tracking_available": "Available from the received status frames",
        "position_tracking_unavailable": (
            "No remote/status tracking identity was discovered. The blind can "
            "still be controlled, but position tracking will rely on Home "
            "Assistant's own commands only."
        ),
        "frame_none_received": "No matching frame received",
        "position_none_recorded": "No position update recorded",
        "diag_title": "Diagnostics:",
        "diag_selected_blind": "Selected device: {value}",
        "diag_section_stick_state": "Stick state:",
        "diag_connected": "Connected: {value}",
        "diag_mode": "Mode: {value}",
        "diag_ready": "Ready: {value}",
        "diag_pairing_active": "Pairing active: {value}",
        "diag_transmitter_active": "Transmitter active: {value}",
        "diag_busy_latched": "Busy latched: {value}",
        "diag_section_ack_semantics": "Acknowledgement semantics:",
        "diag_ack_note_1": (
            "t1/t0 only confirm that the USB stick's own transmitter turned on/off."
        ),
        "diag_ack_note_2": (
            "Whether the motor actually received and moved remains "
            "unverified (the RF link is one-way)."
        ),
        "diag_section_last_matched_frame": "Last matched frame:",
        "diag_device_id": "Device ID: {value}",
        "diag_enum": "Enum: {value}",
        "diag_identity_role": "Identity role: {value}",
        "diag_command": "Command: {value}",
        "diag_interpretation": "Interpretation: {value}",
        "diag_position_tracking_value": "Position tracking: {value}",
        "diag_time": "Time: {value}",
        "diag_section_last_primary_frame": "Last primary tracking frame:",
        "diag_section_last_secondary_frame": "Last secondary frame:",
        "diag_section_last_position_update": "Last position update:",
        "diag_source": "Source: {value}",
        "diag_details": "Details: {value}",
        "diag_direction": "Direction: {value}",
        "diag_previous_position": "Previous position: {value}",
        "diag_new_position": "New position: {value}",
        "diag_status": "Status: {value}",
        "diag_section_position_confidence": "Position confidence:",
        "diag_current_estimated_position": "Current estimated position: {value}",
        "diag_last_manual_sync_time": "Last manual sync time: {value}",
        "diag_confidence": "Confidence: {value}",
        "diag_confirmed_since_restart": "Confirmed since restart: {value}",
        "diag_section_current_transmit_target": "Current transmit target:",
        "diag_section_configured_primary_identity": (
            "Configured primary status identity:"
        ),
        "diag_section_configured_secondary_identities": (
            "Configured secondary status identities:"
        ),
        "diag_section_last_calibration_run": "Last calibration run:",
        "diag_completed": "Completed: {value}",
        "diag_end_reason": "End reason: {value}",
        "diag_frames_observed": "Frames observed during calibration:",
        "diag_candidate_identities": "Candidate status identities:",
        "diag_open_time": "Open time: {value} seconds",
        "diag_close_time": "Close time: {value} seconds",
        "diag_invert_direction": "Invert direction: {value}",
        "cover_open_failed": "Failed to open {name}: {reason}",
        "cover_close_failed": "Failed to close {name}: {reason}",
        "cover_stop_failed": "Failed to stop {name}: {reason}",
        "led_on_failed": "Failed to turn on the LED: {reason}",
        "led_off_failed": "Failed to turn off the LED: {reason}",
        "service_entry_not_loaded": (
            "No loaded Schellenberg USB entry with ID {entry_id}"
        ),
        "service_need_single_entry": (
            "Exactly one Schellenberg USB hub must be loaded, or "
            "config_entry_id must be supplied"
        ),
        "service_command_not_queued": "The serial command could not be queued",
    },
    "de": {
        "no_test_command": "In dieser Sitzung wurde noch kein Testbefehl gesendet.",
        "manually_entered": "manuell eingegeben",
        "not_discovered": "nicht erkannt",
        "none_recorded": "Keine erfasst",
        "none_word": "Keine",
        "never": "Nie",
        "not_recorded": "Nicht erfasst",
        "unknown_label": "Unbekannt",
        "device_fallback_name": "Gerät {device_id}",
        "device_generic": "Gerät",
        "device_model": "USB-Stick",
        "device_model_motor": (
            "USB-Stick-Motor (Befehl {command_device_id}/{command_enum}, "
            "primärer Status {primary_status_text}, "
            "sekundärer Status {secondary_count})"
        ),
        "command_not_sent": "der Befehl wurde nicht gesendet",
        "block_disconnected": "der USB-Stick ist nicht verbunden",
        "block_transport_unavailable": "die serielle Verbindung ist nicht verfügbar",
        "block_transport_closing": "die serielle Verbindung wird gerade geschlossen",
        "block_pairing_active": "eine Kopplung läuft gerade",
        "block_wrong_mode": (
            "der Stick ist im Modus {mode}, benötigt wird aber der Empfangsbereit-Modus"
        ),
        "block_busy_latched": (
            "der Stick hat wiederholt „busy“ gemeldet und benötigt einen Reset"
        ),
        "block_pending_retry": (
            "für die Übertragung {payload} wird noch auf eine Antwort gewartet"
        ),
        "notice_command_blocked": (
            "Befehl „{command}“ blockiert: {reason}. Nutze „Stick "
            "zurücksetzen“ oder verbinde den seriellen Port neu, falls sich "
            "das nicht von selbst löst."
        ),
        "notice_command_sent": "Befehl „{command}“ erfolgreich gesendet.",
        "notice_command_failed": (
            "Befehl „{command}“ fehlgeschlagen; bitte die Protokolle der "
            "Integration prüfen."
        ),
        "notice_position_confirmed": (
            "Position manuell auf {position}% bestätigt. Es wurde kein "
            "Funkbefehl gesendet."
        ),
        "notice_position_sync_unregistered": (
            "Der manuelle Positionsabgleich ist fehlgeschlagen, weil die "
            "aktive Rollladen-Entität nicht registriert ist. Bitte die "
            "Integration neu laden und erneut versuchen."
        ),
        "notice_teach_blocked": "Motor-Anlernen blockiert: {reason}.",
        "notice_teach_test_sent": (
            "Anlernen, Auf und Stopp wurden gesendet. Die Rückmeldungen des "
            "Sticks bestätigen nur die Funkübertragung - bitte prüfen, ob "
            "der Motor tatsächlich reagiert hat."
        ),
        "notice_teach_test_failed": (
            "Anlernen/Test fehlgeschlagen; bitte die Protokolle der Integration prüfen."
        ),
        "test_motor_retry_notice": (
            "Der vorherige Versuch hat keine Bewegung des Rollladens "
            "bestätigt - wählen Sie eine Richtung, um es erneut zu "
            "versuchen."
        ),
        "notice_raw_sent": (
            "Rohdaten-Befehl {payload} wurde gesendet. Die Rückmeldungen des "
            "Sticks bestätigen keine Motorbewegung."
        ),
        "notice_reset_ready": (
            "Stick-Reset und serieller Neuverbindungsaufbau abgeschlossen; "
            "bereit zum Senden."
        ),
        "notice_reset_not_ready": (
            "Stick-Reset/Neuverbindung wurde nicht betriebsbereit "
            "(verbunden={connected}, Modus={mode}). Bitte die Protokolle der "
            "Integration und die USB-Verbindung prüfen."
        ),
        "source_calibration": "automatisch während der Kalibrierung erkannt",
        "source_remote_discovery": (
            "automatisch über die Original-Fernbedienung erkannt"
        ),
        "source_unknown": "unbekannt / noch nicht erkannt",
        "source_legacy": "Altkonfiguration / nicht überprüft",
        "position_tracking_available": "Verfügbar über empfangene Statusmeldungen",
        "position_tracking_unavailable": (
            "Es wurde keine Fernbedienungs-/Status-Identität erkannt. Der "
            "Rollladen lässt sich weiterhin steuern, aber die "
            "Positionsverfolgung stützt sich dann nur auf die Befehle von "
            "Home Assistant selbst."
        ),
        "frame_none_received": "Keine passende Meldung empfangen",
        "position_none_recorded": "Keine Positionsaktualisierung erfasst",
        "diag_title": "Diagnose:",
        "diag_selected_blind": "Ausgewähltes Gerät: {value}",
        "diag_section_stick_state": "Stick-Status:",
        "diag_connected": "Verbunden: {value}",
        "diag_mode": "Modus: {value}",
        "diag_ready": "Bereit: {value}",
        "diag_pairing_active": "Kopplung aktiv: {value}",
        "diag_transmitter_active": "Sender aktiv: {value}",
        "diag_busy_latched": "Busy-Zustand gemeldet: {value}",
        "diag_section_ack_semantics": "Bedeutung der Rückmeldungen:",
        "diag_ack_note_1": (
            "t1/t0 bestätigen nur, dass der Sender des USB-Sticks selbst "
            "ein- bzw. ausgeschaltet wurde."
        ),
        "diag_ack_note_2": (
            "Ob der Motor die Meldung tatsächlich empfangen und sich bewegt "
            "hat, bleibt unbestätigt (die Funkverbindung ist nur in eine "
            "Richtung möglich)."
        ),
        "diag_section_last_matched_frame": "Letzte zugeordnete Meldung:",
        "diag_device_id": "Geräte-ID: {value}",
        "diag_enum": "Enum: {value}",
        "diag_identity_role": "Rolle der Identität: {value}",
        "diag_command": "Befehl: {value}",
        "diag_interpretation": "Interpretation: {value}",
        "diag_position_tracking_value": "Positionsverfolgung: {value}",
        "diag_time": "Zeit: {value}",
        "diag_section_last_primary_frame": "Letzte Primärstatus-Meldung:",
        "diag_section_last_secondary_frame": "Letzte Sekundärstatus-Meldung:",
        "diag_section_last_position_update": "Letzte Positionsaktualisierung:",
        "diag_source": "Quelle: {value}",
        "diag_details": "Details: {value}",
        "diag_direction": "Richtung: {value}",
        "diag_previous_position": "Vorherige Position: {value}",
        "diag_new_position": "Neue Position: {value}",
        "diag_status": "Status: {value}",
        "diag_section_position_confidence": "Verlässlichkeit der Position:",
        "diag_current_estimated_position": "Aktuell geschätzte Position: {value}",
        "diag_last_manual_sync_time": "Letzter manueller Abgleich: {value}",
        "diag_confidence": "Verlässlichkeit: {value}",
        "diag_confirmed_since_restart": "Seit Neustart bestätigt: {value}",
        "diag_section_current_transmit_target": "Aktuelles Sendeziel:",
        "diag_section_configured_primary_identity": (
            "Konfigurierte primäre Status-Identität:"
        ),
        "diag_section_configured_secondary_identities": (
            "Konfigurierte sekundäre Status-Identitäten:"
        ),
        "diag_section_last_calibration_run": "Letzter Kalibrierungslauf:",
        "diag_completed": "Abgeschlossen: {value}",
        "diag_end_reason": "Abbruchgrund: {value}",
        "diag_frames_observed": "Während der Kalibrierung beobachtete Meldungen:",
        "diag_candidate_identities": "Erkannte Kandidaten-Identitäten:",
        "diag_open_time": "Öffnungszeit: {value} Sekunden",
        "diag_close_time": "Schließzeit: {value} Sekunden",
        "diag_invert_direction": "Laufrichtung invertiert: {value}",
        "cover_open_failed": "Öffnen von {name} fehlgeschlagen: {reason}",
        "cover_close_failed": "Schließen von {name} fehlgeschlagen: {reason}",
        "cover_stop_failed": "Stopp von {name} fehlgeschlagen: {reason}",
        "led_on_failed": "LED konnte nicht eingeschaltet werden: {reason}",
        "led_off_failed": "LED konnte nicht ausgeschaltet werden: {reason}",
        "service_entry_not_loaded": (
            "Kein geladener Schellenberg-USB-Eintrag mit der ID {entry_id}"
        ),
        "service_need_single_entry": (
            "Es muss genau ein Schellenberg-USB-Hub geladen sein, oder "
            "config_entry_id muss angegeben werden"
        ),
        "service_command_not_queued": (
            "Der serielle Befehl konnte nicht eingereiht werden"
        ),
    },
    "es": {
        "no_test_command": "Todavía no se ha enviado ningún comando de prueba "
        "en esta sesión.",
        "manually_entered": "introducida manualmente",
        "not_discovered": "no detectada",
        "none_recorded": "Ninguna registrada",
        "none_word": "Ninguna",
        "never": "Nunca",
        "not_recorded": "No registrado",
        "unknown_label": "Desconocido",
        "device_fallback_name": "Dispositivo {device_id}",
        "device_generic": "Dispositivo",
        "device_model": "Stick USB",
        "device_model_motor": (
            "Motor de memoria USB (comando {command_device_id}/{command_enum}, "
            "estado principal {primary_status_text}, "
            "estados secundarios {secondary_count})"
        ),
        "command_not_sent": "el comando no se envió",
        "block_disconnected": "el stick USB está desconectado",
        "block_transport_unavailable": "la conexión serie no está disponible",
        "block_transport_closing": "la conexión serie se está cerrando",
        "block_pairing_active": "hay un emparejamiento en curso",
        "block_wrong_mode": (
            "el stick está en modo {mode}, pero se necesita el modo de escucha"
        ),
        "block_busy_latched": (
            "el stick notificó estar ocupado varias veces seguidas y "
            "necesita un reinicio"
        ),
        "block_pending_retry": (
            "todavía se está esperando la confirmación de la transmisión {payload}"
        ),
        "notice_command_blocked": (
            'Comando "{command}" bloqueado: {reason}. Usa "Reiniciar stick" '
            "o reconecta el puerto serie si esto no se resuelve por sí solo."
        ),
        "notice_command_sent": 'Comando "{command}" enviado correctamente.',
        "notice_command_failed": (
            'El comando "{command}" ha fallado; revisa los registros de la integración.'
        ),
        "notice_position_confirmed": (
            "Posición confirmada manualmente al {position}%. No se envió "
            "ningún comando de radiofrecuencia."
        ),
        "notice_position_sync_unregistered": (
            "La sincronización manual de posición falló porque la entidad "
            "de la persiana activa no está registrada. Recarga la "
            "integración e inténtalo de nuevo."
        ),
        "notice_teach_blocked": "Aprendizaje del motor bloqueado: {reason}.",
        "notice_teach_test_sent": (
            "Se enviaron Aprender, Abrir y Detener. Las confirmaciones del "
            "stick solo acreditan la transmisión por radio - comprueba si "
            "el motor realmente reaccionó."
        ),
        "notice_teach_test_failed": (
            "Falló la transmisión de aprendizaje/prueba; revisa los "
            "registros de la integración."
        ),
        "test_motor_retry_notice": (
            "El intento anterior no confirmó movimiento de la persiana - "
            "elija una dirección para intentarlo de nuevo."
        ),
        "notice_raw_sent": (
            "Se envió la carga en bruto {payload}. Las confirmaciones del "
            "stick no acreditan movimiento del motor."
        ),
        "notice_reset_ready": (
            "Reinicio del stick y reconexión serie completados; listo para transmitir."
        ),
        "notice_reset_not_ready": (
            "El reinicio/reconexión del stick no quedó listo "
            "(conectado={connected}, modo={mode}). Revisa los registros de "
            "la integración y la conexión USB."
        ),
        "source_calibration": "detectada automáticamente durante la calibración",
        "source_remote_discovery": (
            "detectada automáticamente desde el mando original"
        ),
        "source_unknown": "desconocida / aún no detectada",
        "source_legacy": "configuración antigua / sin verificar",
        "position_tracking_available": (
            "Disponible a partir de las tramas de estado recibidas"
        ),
        "position_tracking_unavailable": (
            "No se detectó ninguna identidad de mando/estado. La persiana "
            "se puede seguir controlando, pero el seguimiento de posición "
            "dependerá solo de los comandos de Home Assistant."
        ),
        "frame_none_received": "No se recibió ninguna trama coincidente",
        "position_none_recorded": "No se registró ninguna actualización de posición",
        "diag_title": "Diagnóstico:",
        "diag_selected_blind": "Dispositivo seleccionado: {value}",
        "diag_section_stick_state": "Estado del stick:",
        "diag_connected": "Conectado: {value}",
        "diag_mode": "Modo: {value}",
        "diag_ready": "Listo: {value}",
        "diag_pairing_active": "Emparejamiento activo: {value}",
        "diag_transmitter_active": "Transmisor activo: {value}",
        "diag_busy_latched": "Estado ocupado notificado: {value}",
        "diag_section_ack_semantics": "Significado de las confirmaciones:",
        "diag_ack_note_1": (
            "t1/t0 solo confirman que el propio transmisor del stick USB "
            "se encendió o apagó."
        ),
        "diag_ack_note_2": (
            "Si el motor realmente recibió la orden y se movió sigue sin "
            "poder confirmarse (el enlace de radio es unidireccional)."
        ),
        "diag_section_last_matched_frame": "Última trama coincidente:",
        "diag_device_id": "ID de dispositivo: {value}",
        "diag_enum": "Enum: {value}",
        "diag_identity_role": "Rol de la identidad: {value}",
        "diag_command": "Comando: {value}",
        "diag_interpretation": "Interpretación: {value}",
        "diag_position_tracking_value": "Seguimiento de posición: {value}",
        "diag_time": "Hora: {value}",
        "diag_section_last_primary_frame": "Última trama de estado primario:",
        "diag_section_last_secondary_frame": "Última trama de estado secundario:",
        "diag_section_last_position_update": "Última actualización de posición:",
        "diag_source": "Origen: {value}",
        "diag_details": "Detalles: {value}",
        "diag_direction": "Dirección: {value}",
        "diag_previous_position": "Posición anterior: {value}",
        "diag_new_position": "Posición nueva: {value}",
        "diag_status": "Estado: {value}",
        "diag_section_position_confidence": "Fiabilidad de la posición:",
        "diag_current_estimated_position": "Posición estimada actual: {value}",
        "diag_last_manual_sync_time": "Última sincronización manual: {value}",
        "diag_confidence": "Fiabilidad: {value}",
        "diag_confirmed_since_restart": "Confirmada desde el reinicio: {value}",
        "diag_section_current_transmit_target": "Destino de transmisión actual:",
        "diag_section_configured_primary_identity": (
            "Identidad de estado primaria configurada:"
        ),
        "diag_section_configured_secondary_identities": (
            "Identidades de estado secundarias configuradas:"
        ),
        "diag_section_last_calibration_run": "Última calibración realizada:",
        "diag_completed": "Completada: {value}",
        "diag_end_reason": "Motivo de finalización: {value}",
        "diag_frames_observed": "Tramas observadas durante la calibración:",
        "diag_candidate_identities": "Identidades candidatas detectadas:",
        "diag_open_time": "Tiempo de apertura: {value} segundos",
        "diag_close_time": "Tiempo de cierre: {value} segundos",
        "diag_invert_direction": "Dirección invertida: {value}",
        "cover_open_failed": "No se pudo abrir {name}: {reason}",
        "cover_close_failed": "No se pudo cerrar {name}: {reason}",
        "cover_stop_failed": "No se pudo detener {name}: {reason}",
        "led_on_failed": "No se pudo encender el LED: {reason}",
        "led_off_failed": "No se pudo apagar el LED: {reason}",
        "service_entry_not_loaded": (
            "No hay ninguna entrada de Schellenberg USB cargada con el ID {entry_id}"
        ),
        "service_need_single_entry": (
            "Debe haber exactamente un hub de Schellenberg USB cargado, o "
            "se debe indicar config_entry_id"
        ),
        "service_command_not_queued": "No se pudo encolar el comando serie",
    },
    "fr": {
        "no_test_command": (
            "Aucune commande de test n'a encore été envoyée au cours de cette session."
        ),
        "manually_entered": "saisie manuellement",
        "not_discovered": "non détectée",
        "none_recorded": "Aucune enregistrée",
        "none_word": "Aucune",
        "never": "Jamais",
        "not_recorded": "Non renseigné",
        "unknown_label": "Inconnu",
        "device_fallback_name": "Appareil {device_id}",
        "device_generic": "Appareil",
        "device_model": "Clé USB",
        "device_model_motor": (
            "Moteur clé USB (commande {command_device_id}/{command_enum}, "
            "statut principal {primary_status_text}, "
            "statuts secondaires {secondary_count})"
        ),
        "command_not_sent": "la commande n'a pas été envoyée",
        "block_disconnected": "la clé USB n'est pas connectée",
        "block_transport_unavailable": "la liaison série n'est pas disponible",
        "block_transport_closing": "la liaison série est en cours de fermeture",
        "block_pairing_active": "un appairage est en cours",
        "block_wrong_mode": (
            "la clé est en mode {mode}, alors que le mode à l'écoute est nécessaire"
        ),
        "block_busy_latched": (
            "la clé a signalé un état occupé de façon répétée et a besoin "
            "d'une réinitialisation"
        ),
        "block_pending_retry": (
            "une transmission est toujours en attente pour la charge utile {payload}"
        ),
        "notice_command_blocked": (
            "Commande « {command} » bloquée : {reason}. Utilisez "
            "« Réinitialiser la clé » ou reconnectez le port série si cela "
            "ne se résout pas tout seul."
        ),
        "notice_command_sent": "Commande « {command} » envoyée avec succès.",
        "notice_command_failed": (
            "La commande « {command} » a échoué ; consultez les journaux "
            "de l'intégration."
        ),
        "notice_position_confirmed": (
            "Position confirmée manuellement à {position} %. Aucune "
            "commande radio n'a été envoyée."
        ),
        "notice_position_sync_unregistered": (
            "La synchronisation manuelle de position a échoué car l'entité "
            "volet active n'est pas enregistrée. Rechargez l'intégration "
            "et réessayez."
        ),
        "notice_teach_blocked": "Apprentissage du moteur bloqué : {reason}.",
        "notice_teach_test_sent": (
            "Apprentissage, Ouverture et Arrêt ont été transmis. Les accusés "
            "de réception de la clé confirment uniquement la transmission "
            "radio - vérifiez que le moteur a réellement réagi."
        ),
        "notice_teach_test_failed": (
            "La transmission d'apprentissage/de test a échoué ; consultez "
            "les journaux de l'intégration."
        ),
        "test_motor_retry_notice": (
            "La tentative précédente n'a pas confirmé de mouvement du "
            "volet - choisissez une direction pour réessayer."
        ),
        "notice_raw_sent": (
            "La charge utile brute {payload} a été envoyée. Les accusés de "
            "réception de la clé ne confirment pas le mouvement du moteur."
        ),
        "notice_reset_ready": (
            "Réinitialisation de la clé et reconnexion série terminées ; "
            "prête à transmettre."
        ),
        "notice_reset_not_ready": (
            "La réinitialisation/reconnexion de la clé n'a pas abouti "
            "(connectée={connected}, mode={mode}). Consultez les journaux "
            "de l'intégration et la connexion USB."
        ),
        "source_calibration": "détectée automatiquement pendant le calibrage",
        "source_remote_discovery": (
            "détectée automatiquement depuis la télécommande d'origine"
        ),
        "source_unknown": "inconnue / pas encore détectée",
        "source_legacy": "configuration héritée / non vérifiée",
        "position_tracking_available": ("Disponible à partir des trames d'état reçues"),
        "position_tracking_unavailable": (
            "Aucune identité de télécommande/état n'a été détectée. Le "
            "volet peut toujours être commandé, mais le suivi de position "
            "ne reposera plus que sur les commandes de Home Assistant."
        ),
        "frame_none_received": "Aucune trame correspondante reçue",
        "position_none_recorded": "Aucune mise à jour de position enregistrée",
        "diag_title": "Diagnostic :",
        "diag_selected_blind": "Appareil sélectionné : {value}",
        "diag_section_stick_state": "État de la clé :",
        "diag_connected": "Connectée : {value}",
        "diag_mode": "Mode : {value}",
        "diag_ready": "Prête : {value}",
        "diag_pairing_active": "Appairage en cours : {value}",
        "diag_transmitter_active": "Émetteur actif : {value}",
        "diag_busy_latched": "État occupé signalé : {value}",
        "diag_section_ack_semantics": "Signification des accusés de réception :",
        "diag_ack_note_1": (
            "t1/t0 confirment seulement que l'émetteur de la clé USB "
            "elle-même s'est allumé/éteint."
        ),
        "diag_ack_note_2": (
            "La réception et le mouvement effectifs du moteur restent non "
            "vérifiés (la liaison radio est à sens unique)."
        ),
        "diag_section_last_matched_frame": "Dernière trame associée :",
        "diag_device_id": "ID d'appareil : {value}",
        "diag_enum": "Enum : {value}",
        "diag_identity_role": "Rôle de l'identité : {value}",
        "diag_command": "Commande : {value}",
        "diag_interpretation": "Interprétation : {value}",
        "diag_position_tracking_value": "Suivi de position : {value}",
        "diag_time": "Heure : {value}",
        "diag_section_last_primary_frame": "Dernière trame d'état principal :",
        "diag_section_last_secondary_frame": "Dernière trame d'état secondaire :",
        "diag_section_last_position_update": "Dernière mise à jour de position :",
        "diag_source": "Source : {value}",
        "diag_details": "Détails : {value}",
        "diag_direction": "Direction : {value}",
        "diag_previous_position": "Position précédente : {value}",
        "diag_new_position": "Nouvelle position : {value}",
        "diag_status": "Statut : {value}",
        "diag_section_position_confidence": "Fiabilité de la position :",
        "diag_current_estimated_position": "Position estimée actuelle : {value}",
        "diag_last_manual_sync_time": "Dernière synchronisation manuelle : {value}",
        "diag_confidence": "Fiabilité : {value}",
        "diag_confirmed_since_restart": "Confirmée depuis le redémarrage : {value}",
        "diag_section_current_transmit_target": "Cible de transmission actuelle :",
        "diag_section_configured_primary_identity": (
            "Identité d'état principale configurée :"
        ),
        "diag_section_configured_secondary_identities": (
            "Identités d'état secondaires configurées :"
        ),
        "diag_section_last_calibration_run": "Dernier calibrage effectué :",
        "diag_completed": "Terminé : {value}",
        "diag_end_reason": "Motif de fin : {value}",
        "diag_frames_observed": "Trames observées pendant le calibrage :",
        "diag_candidate_identities": "Identités candidates détectées :",
        "diag_open_time": "Temps d'ouverture : {value} secondes",
        "diag_close_time": "Temps de fermeture : {value} secondes",
        "diag_invert_direction": "Sens inversé : {value}",
        "cover_open_failed": "Impossible d'ouvrir {name} : {reason}",
        "cover_close_failed": "Impossible de fermer {name} : {reason}",
        "cover_stop_failed": "Impossible d'arrêter {name} : {reason}",
        "led_on_failed": "Impossible d'allumer la LED : {reason}",
        "led_off_failed": "Impossible d'éteindre la LED : {reason}",
        "service_entry_not_loaded": (
            "Aucune entrée Schellenberg USB chargée avec l'ID {entry_id}"
        ),
        "service_need_single_entry": (
            "Exactement un hub Schellenberg USB doit être chargé, ou "
            "config_entry_id doit être fourni"
        ),
        "service_command_not_queued": (
            "La commande série n'a pas pu être mise en file d'attente"
        ),
    },
}

_PHRASE_TRANSLATIONS: dict[str, dict[str, str]] = {
    "de": {
        "Yes": "Ja",
        "No": "Nein",
        "open": "Öffnen",
        "close": "Schließen",
        "stop": "Stopp",
        "unknown": "unbekannt",
        "primary": "primär",
        "secondary": "sekundär",
        "unmatched": "nicht zugeordnet",
        "none": "keine",
        "bootloader": "Bootloader",
        "initial": "Initial",
        "listening": "Empfangsbereit",
        "pairing": "Kopplung",
        "idle": "inaktiv",
        "idle_between_legs": "inaktiv zwischen den Phasen",
        "remote_discovery": "Fernbedienungs-Erkennung",
        "opening_endstop": "öffnend, Endanschlag erreicht",
        "closing_endstop": "schließend, Endanschlag erreicht",
        "manual": "manuell",
        "opening": "öffnend",
        "closing": "schließend",
        "confirmed/manual": "bestätigt/manuell",
        "confirmed": "bestätigt",
        "estimated": "geschätzt",
        "estimated from full travel": "geschätzt anhand vollständiger Fahrt",
        "restored / estimated / not confirmed since restart": (
            "wiederhergestellt / geschätzt / seit Neustart nicht bestätigt"
        ),
        "estimated / not confirmed since restart": (
            "geschätzt / seit Neustart nicht bestätigt"
        ),
        "measured during pairing / not confirmed since restart": (
            "gemessen während der Kopplung / seit Neustart nicht bestätigt"
        ),
        "not recorded": "nicht erfasst",
        "startup default": "Startvorgabe",
        "restored HA state": "aus Home-Assistant-Zustand wiederhergestellt",
        "manual sync": "manueller Abgleich",
        "calibration": "Kalibrierung",
        "primary status": "Primärstatus",
        "HA command": "Home-Assistant-Befehl",
        "Home Assistant open command": "Home-Assistant-Befehl „Öffnen“",
        "Home Assistant close command": "Home-Assistant-Befehl „Schließen“",
        "Home Assistant stop command": "Home-Assistant-Befehl „Stopp“",
        "Developer Tools manual position sync": (
            "Manueller Positionsabgleich über die Entwicklerwerkzeuge"
        ),
        "completed calibration": "abgeschlossene Kalibrierung",
        "fresh calibration result": "frisches Kalibrierungsergebnis",
        "startup default (no restored HA state)": (
            "Startvorgabe (kein wiederhergestellter Home-Assistant-Zustand)"
        ),
        "completed": "abgeschlossen",
        "cancelled": "abgebrochen",
        "capture_timeout_complete": "Erfassungszeit abgelaufen",
        "opening_start_timeout": "Zeitüberschreitung beim Start des Öffnens",
        "opening_stop_timeout": "Zeitüberschreitung beim Stopp des Öffnens",
        "opening_cancelled": "Öffnen abgebrochen",
        "opening_error": "Fehler beim Öffnen",
        "closing_start_timeout": "Zeitüberschreitung beim Start des Schließens",
        "closing_stop_timeout": "Zeitüberschreitung beim Stopp des Schließens",
        "closing_cancelled": "Schließen abgebrochen",
        "closing_error": "Fehler beim Schließen",
    },
    "es": {
        "Yes": "Sí",
        "No": "No",
        "open": "Abrir",
        "close": "Cerrar",
        "stop": "Detener",
        "unknown": "desconocido",
        "primary": "primaria",
        "secondary": "secundaria",
        "unmatched": "sin asignar",
        "none": "ninguna",
        "bootloader": "Bootloader",
        "initial": "Inicial",
        "listening": "Escuchando",
        "pairing": "Emparejamiento",
        "idle": "inactiva",
        "idle_between_legs": "inactiva entre fases",
        "remote_discovery": "detección del mando",
        "opening_endstop": "abriendo, final de recorrido alcanzado",
        "closing_endstop": "cerrando, final de recorrido alcanzado",
        "manual": "manual",
        "opening": "abriendo",
        "closing": "cerrando",
        "confirmed/manual": "confirmada/manual",
        "confirmed": "confirmada",
        "estimated": "estimada",
        "estimated from full travel": "estimada a partir del recorrido completo",
        "restored / estimated / not confirmed since restart": (
            "restaurada / estimada / no confirmada desde el reinicio"
        ),
        "estimated / not confirmed since restart": (
            "estimada / no confirmada desde el reinicio"
        ),
        "measured during pairing / not confirmed since restart": (
            "medida durante el emparejamiento / no confirmada desde el reinicio"
        ),
        "not recorded": "no registrado",
        "startup default": "valor predeterminado al iniciar",
        "restored HA state": "estado restaurado de Home Assistant",
        "manual sync": "sincronización manual",
        "calibration": "calibración",
        "primary status": "estado primario",
        "HA command": "comando de Home Assistant",
        "Home Assistant open command": "Comando de apertura de Home Assistant",
        "Home Assistant close command": "Comando de cierre de Home Assistant",
        "Home Assistant stop command": "Comando de parada de Home Assistant",
        "Developer Tools manual position sync": (
            "Sincronización manual de posición desde Herramientas de desarrollo"
        ),
        "completed calibration": "calibración completada",
        "fresh calibration result": "resultado reciente de la calibración",
        "startup default (no restored HA state)": (
            "valor predeterminado al iniciar (sin estado restaurado de Home Assistant)"
        ),
        "completed": "completada",
        "cancelled": "cancelada",
        "capture_timeout_complete": "tiempo de captura agotado",
        "opening_start_timeout": "tiempo de espera agotado al iniciar la apertura",
        "opening_stop_timeout": "tiempo de espera agotado al detener la apertura",
        "opening_cancelled": "apertura cancelada",
        "opening_error": "error durante la apertura",
        "closing_start_timeout": "tiempo de espera agotado al iniciar el cierre",
        "closing_stop_timeout": "tiempo de espera agotado al detener el cierre",
        "closing_cancelled": "cierre cancelado",
        "closing_error": "error durante el cierre",
    },
    "fr": {
        "Yes": "Oui",
        "No": "Non",
        "open": "Ouvrir",
        "close": "Fermer",
        "stop": "Arrêter",
        "unknown": "inconnu",
        "primary": "principale",
        "secondary": "secondaire",
        "unmatched": "non associée",
        "none": "aucune",
        "bootloader": "Bootloader",
        "initial": "Initial",
        "listening": "À l'écoute",
        "pairing": "Appairage",
        "idle": "inactive",
        "idle_between_legs": "inactive entre les phases",
        "remote_discovery": "détection de la télécommande",
        "opening_endstop": "ouverture, butée atteinte",
        "closing_endstop": "fermeture, butée atteinte",
        "manual": "manuel",
        "opening": "ouverture",
        "closing": "fermeture",
        "confirmed/manual": "confirmée/manuelle",
        "confirmed": "confirmée",
        "estimated": "estimée",
        "estimated from full travel": "estimée sur un trajet complet",
        "restored / estimated / not confirmed since restart": (
            "restaurée / estimée / non confirmée depuis le redémarrage"
        ),
        "estimated / not confirmed since restart": (
            "estimée / non confirmée depuis le redémarrage"
        ),
        "measured during pairing / not confirmed since restart": (
            "mesurée pendant l'appairage / non confirmée depuis le redémarrage"
        ),
        "not recorded": "non enregistré",
        "startup default": "valeur par défaut au démarrage",
        "restored HA state": "état restauré de Home Assistant",
        "manual sync": "synchronisation manuelle",
        "calibration": "calibrage",
        "primary status": "état principal",
        "HA command": "commande Home Assistant",
        "Home Assistant open command": "Commande d'ouverture Home Assistant",
        "Home Assistant close command": "Commande de fermeture Home Assistant",
        "Home Assistant stop command": "Commande d'arrêt Home Assistant",
        "Developer Tools manual position sync": (
            "Synchronisation manuelle de position via les outils de développement"
        ),
        "completed calibration": "calibrage terminé",
        "fresh calibration result": "résultat récent du calibrage",
        "startup default (no restored HA state)": (
            "valeur par défaut au démarrage (aucun état Home Assistant restauré)"
        ),
        "completed": "terminé",
        "cancelled": "annulé",
        "capture_timeout_complete": "délai de capture écoulé",
        "opening_start_timeout": "délai dépassé au début de l'ouverture",
        "opening_stop_timeout": "délai dépassé à l'arrêt de l'ouverture",
        "opening_cancelled": "ouverture annulée",
        "opening_error": "erreur pendant l'ouverture",
        "closing_start_timeout": "délai dépassé au début de la fermeture",
        "closing_stop_timeout": "délai dépassé à l'arrêt de la fermeture",
        "closing_cancelled": "fermeture annulée",
        "closing_error": "erreur pendant la fermeture",
    },
}


def _language(hass: HomeAssistant | None) -> str:
    """Return the configured Home Assistant language, or English."""
    if hass is None:
        return "en"
    return hass.config.language or "en"


def runtime_translation_text(
    hass: HomeAssistant | None, key: str, **kwargs: Any
) -> str:
    """Return one parameterized runtime-text template, filled in.

    Falls back to the English template (and, failing that, to the key
    itself) if a language or key is somehow missing, so a translation gap
    never raises - it just shows English instead of crashing the flow.
    """
    language = _language(hass)
    table = _RUNTIME_TRANSLATION_TEXT.get(language, _RUNTIME_TRANSLATION_TEXT["en"])
    template = table.get(key, _RUNTIME_TRANSLATION_TEXT["en"].get(key, key))
    return template.format(**kwargs) if kwargs else template


def translate_phrase(hass: HomeAssistant | None, phrase: str) -> str:
    """Translate one short, reusable literal English phrase.

    Used for values that are produced and compared elsewhere as plain
    English literals (cover.py's internal position/status bookkeeping,
    api.py's device_mode/identity_role strings, "Yes"/"No") and only need
    translating at the point they reach the UI. Returns the original
    phrase unchanged if there is no translation for it (including when
    already in English), so an unrecognized or future value degrades to
    English instead of disappearing.
    """
    language = _language(hass)
    if language == "en":
        return phrase
    return _PHRASE_TRANSLATIONS.get(language, {}).get(phrase, phrase)


def yes_no(hass: HomeAssistant | None, value: bool) -> str:
    """Translate a boolean to a localized "Yes"/"No"."""
    return translate_phrase(hass, "Yes" if value else "No")


_BLOCK_REASON_KEYS = {
    "disconnected": "block_disconnected",
    "transport_unavailable": "block_transport_unavailable",
    "transport_closing": "block_transport_closing",
    "pairing_active": "block_pairing_active",
    "busy_latched": "block_busy_latched",
}


def translate_block_reason(hass: HomeAssistant | None, code: str | None) -> str | None:
    """Translate one of api.py's stable transmit_block_reason codes.

    api.py's _transmit_capability_block_reason()/transmit_block_reason
    return a short, machine-stable code (e.g. "disconnected",
    "wrong_mode:initial", "pending_retry:tr109...") rather than a
    ready-made English sentence, specifically so this function - called
    only at the UI layer - can show it in the user's own language. Returns
    None unchanged so callers can keep using `translate_block_reason(...)
    or <fallback>` the same way they used the raw code before.
    """
    if code is None:
        return None
    base, _, value = code.partition(":")
    if base == "wrong_mode":
        return runtime_translation_text(
            hass, "block_wrong_mode", mode=translate_phrase(hass, value)
        )
    if base == "pending_retry":
        return runtime_translation_text(hass, "block_pending_retry", payload=value)
    key = _BLOCK_REASON_KEYS.get(base)
    if key is not None:
        return runtime_translation_text(hass, key)
    return code
