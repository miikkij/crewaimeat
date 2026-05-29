"""LLM-factory.

Oletuksena käytetään OpenRouteria (yksi avain, monta mallia).
Vaihtoehtoisesti voi kutsua xAI:ta (Grok) suoraan asettamalla USE_XAI=1.
Molemmat menevät CrewAI:n LLM-luokan (litellm) kautta.
"""

from __future__ import annotations

import os

from crewai import LLM


def get_llm() -> LLM:
    """Rakentaa LLM-instanssin ympäristömuuttujien perusteella."""
    temperature = float(os.getenv("LLM_TEMPERATURE", "0.5"))

    # --- Vaihtoehto: xAI suoraan ---------------------------------------
    if os.getenv("USE_XAI") not in (None, "", "0", "false", "False"):
        api_key = os.getenv("XAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "USE_XAI on päällä mutta XAI_API_KEY puuttuu. "
                "Täytä avain .env-tiedostoon."
            )
        model = os.getenv("XAI_MODEL", "xai/grok-4-fast")
        # litellm lukee XAI_API_KEY:n automaattisesti; annetaan silti eksplisiittisesti.
        return LLM(model=model, api_key=api_key, temperature=temperature)

    # --- Oletus: OpenRouter --------------------------------------------
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY puuttuu. Kopioi .env.example -> .env ja täytä avain "
            "(tai aseta USE_XAI=1 käyttääksesi xAI:ta suoraan)."
        )
    model = os.getenv("OPENROUTER_MODEL", "openrouter/x-ai/grok-4-fast")
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    return LLM(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
    )
