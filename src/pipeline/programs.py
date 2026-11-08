"""New-grad / rotational / early-career program titles (shared by the rules and the Sheet)."""
import re

PROGRAM_RX = re.compile(
    r"\b(?:new grad(?:uate)?s?|graduate (?:program|leadership|scheme|rotational|associate)|grad program|"
    r"university (?:hire|graduate|grad)|campus (?:hire|full[- ]time)|rotational|rotation program|"
    r"(?:leadership|career|operations|management|business|analyst|associate) development program|"
    r"leadership program|early[- ]career|emerging talent|class of 202[6-8]|202[78] (?:start|graduates?|new grad)|"
    r"wolf graduate|associates? program|analyst program|fellowship)\b", re.I)
