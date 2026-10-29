"""AI agents: provider registry, credentials (own key or the "default key" tag),
an LLM client shared by scraping / scripting agents, and connection tests.

Kinds and providers
- calling:   twilio_gemini (Twilio dials, Gemini Live speaks)  |  bland (Bland AI)
- scraping:  gemini | openai | anthropic | openai_compatible
- scripting: gemini | openai | anthropic | openai_compatible

Default keys (the "default key" tag, ``use_default_key=True``)
- twilio_gemini → Settings Twilio account (SID, auth token, from number); Gemini
  runs on this app's Google Cloud service account
- bland → ``bland_api_key``          - gemini → the app's Google Cloud (no key)
- openai → ``openai_api_key``        - anthropic → ``anthropic_api_key``
- openai_compatible → ``llm_api_key`` + ``llm_base_url`` (+ ``llm_model``)
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from app.models import Agent

KINDS = ("calling", "scraping", "scripting")

PROVIDERS: dict[str, dict] = {
    "twilio_gemini": {
        "label": "Twilio + Gemini Live", "kinds": ["calling"], "fields": ["account_sid", "api_key", "from_number"],
        "key_label": "Twilio Auth Token", "default_model": "gemini-3.8-live",
        "help": "Twilio dials the number; Gemini Live (this app's Google Cloud) speaks the call flow.",
    },
    "bland": {
        "label": "Bland AI", "kinds": ["calling"], "fields": ["api_key"], "key_label": "Bland API Key",
        "default_model": None, "help": "Bland places the call and runs the agent.",
    },
    "gemini": {
        "label": "Google Gemini (Vertex AI)", "kinds": ["scraping", "scripting"], "fields": [],
        "key_label": None, "default_model": "gemini-2.5-flash",
        "help": "Runs on this app's Google Cloud service account — no key needed.",
    },
    "openai": {
        "label": "OpenAI", "kinds": ["scraping", "scripting"], "fields": ["api_key"], "key_label": "OpenAI API Key",
        "default_model": "gpt-4o-mini", "help": "platform.openai.com/api-keys",
    },
    "anthropic": {
        "label": "Anthropic Claude", "kinds": ["scraping", "scripting"], "fields": ["api_key"],
        "key_label": "Anthropic API Key", "default_model": "claude-sonnet-4-5", "help": "console.anthropic.com",
    },
    "openai_compatible": {
        "label": "OpenAI-compatible (Groq, Together, Ollama, …)", "kinds": ["scraping", "scripting"],
        "fields": ["api_key", "base_url"], "key_label": "API Key", "default_model": None,
        "help": "Any /chat/completions endpoint.",
    },
}

LLM_PROVIDERS = {p for p, d in PROVIDERS.items() if "scraping" in d["kinds"]}


class AgentError(RuntimeError):
    """Agent misconfigured, missing a key, or its provider rejected the request."""


def validate(kind: str, provider: str) -> None:
    if kind not in KINDS:
        raise AgentError(f"kind must be one of {', '.join(KINDS)}")
    if provider not in PROVIDERS:
        raise AgentError(f"Unknown provider '{provider}'")
    if kind not in PROVIDERS[provider]["kinds"]:
        raise AgentError(f"{PROVIDERS[provider]['label']} can't be used for {kind} agents")


def default_credentials(db: Session, provider: str) -> dict:
    from app.services.settings import get_setting, get_twilio_config
    g = lambda k: (get_setting(db, k) or "").strip()  # noqa: E731
    if provider == "twilio_gemini":
        t = get_twilio_config(db)
        return {"account_sid": t["account_sid"], "api_key": t["auth_token"], "from_number": t["from_number"]}
    if provider == "bland":
        return {"api_key": g("bland_api_key")}
    if provider == "openai":
        return {"api_key": g("openai_api_key")}
    if provider == "anthropic":
        return {"api_key": g("anthropic_api_key")}
    if provider == "openai_compatible":
        return {"api_key": g("llm_api_key"), "base_url": g("llm_base_url") or "https://api.openai.com/v1",
                "model": g("llm_model")}
    return {}


def credentials(db: Session, agent: Agent) -> dict:
    """The credentials this agent uses — its own, or the provider default when tagged."""
    if agent.use_default_key:
        creds = default_credentials(db, agent.provider)
        source = "default key"
    else:
        creds = {"api_key": agent.api_key or "", "account_sid": agent.account_sid or "",
                 "from_number": agent.from_number or "", "base_url": agent.base_url or ""}
        source = "own key"
    missing = [f for f in PROVIDERS[agent.provider]["fields"] if not (creds.get(f) or "").strip()]
    if missing:
        where = ("the provider's default key isn't set — add it under Agents → Default keys"
                 if agent.use_default_key else "add it to the agent, or tag it 'default key'")
        raise AgentError(f"{agent.name}: missing {', '.join(missing)} ({source}) — {where}.")
    return creds


def model_for(db: Session, agent: Agent) -> str:
    if agent.model:
        return agent.model
    if agent.provider == "openai_compatible":
        m = default_credentials(db, "openai_compatible").get("model")
        if m:
            return m
    m = PROVIDERS[agent.provider]["default_model"]
    if not m:
        raise AgentError(f"{agent.name}: set a model name.")
    return m


def key_status(db: Session, agent: Agent) -> dict:
    """Safe-to-show credential status (never the key itself)."""
    try:
        credentials(db, agent)
        return {"ok": True, "error": None}
    except AgentError as exc:
        return {"ok": False, "error": str(exc)}


# ── LLM client ──────────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


def complete(db: Session, agent: Agent, system: str, user: str, *, json_mode: bool = True,
             max_tokens: int = 4000, timeout: float = 90.0) -> str:
    """One prompt → text from the agent's LLM. Raises AgentError on any failure."""
    if agent.provider not in LLM_PROVIDERS:
        raise AgentError(f"{agent.name} is a calling agent, not a text model.")
    creds = credentials(db, agent)
    model = model_for(db, agent)
    try:
        if agent.provider == "gemini":
            from google.genai import types
            from app.services.live_agent import client
            genai = client(db)  # keep a reference: the SDK closes its HTTP client on GC
            # 2.5 models think by default and the thinking counts against max_output_tokens —
            # uncapped, it can eat the whole budget and truncate long JSON extractions.
            thinking = types.ThinkingConfig(thinking_budget=1024) if "2.5" in model else None
            cfg = types.GenerateContentConfig(system_instruction=system, temperature=0.2, max_output_tokens=max_tokens,
                                              response_mime_type="application/json" if json_mode else None,
                                              thinking_config=thinking)
            text = genai.models.generate_content(model=model, contents=user, config=cfg).text or ""
        elif agent.provider == "anthropic":
            resp = httpx.post("https://api.anthropic.com/v1/messages", timeout=timeout, headers={
                "x-api-key": creds["api_key"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": model, "max_tokens": max_tokens, "temperature": 0.2,
                      "system": system + ("\n\nRespond with JSON only." if json_mode else ""),
                      "messages": [{"role": "user", "content": user}]})
            if resp.status_code >= 400:
                raise AgentError(f"Claude error {resp.status_code}: {resp.text[:300]}")
            text = "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")
        else:  # openai / openai_compatible
            base = (creds.get("base_url") or "https://api.openai.com/v1").rstrip("/")
            body: dict[str, Any] = {"model": model, "temperature": 0.2, "max_tokens": max_tokens,
                                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            resp = httpx.post(f"{base}/chat/completions", timeout=timeout, json=body,
                              headers={"Authorization": f"Bearer {creds['api_key']}"})
            if resp.status_code >= 400:
                raise AgentError(f"{PROVIDERS[agent.provider]['label']} error {resp.status_code}: {resp.text[:300]}")
            text = resp.json()["choices"][0]["message"]["content"] or ""
    except AgentError:
        raise
    except httpx.HTTPError as exc:
        raise AgentError(f"{PROVIDERS[agent.provider]['label']} unreachable: {exc}") from exc
    except Exception as exc:  # SDK / auth errors
        raise AgentError(f"{PROVIDERS[agent.provider]['label']}: {exc}") from exc
    return _strip_fences(text)


def complete_json(db: Session, agent: Agent, system: str, user: str, **kw) -> Any:
    text = complete(db, agent, system, user, json_mode=True, **kw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        raise AgentError(f"{agent.name} didn't return valid JSON.")


# ── Connection test ─────────────────────────────────────────────────────────

def test(db: Session, agent: Agent) -> dict:
    """Prove the agent's credentials work with a cheap real request."""
    creds = credentials(db, agent)
    if agent.provider in LLM_PROVIDERS:
        data = complete_json(db, agent, "You are a connectivity check.", 'Reply with exactly {"ok": true}', max_tokens=50)
        ok = isinstance(data, dict) and data.get("ok") is True
        return {"ok": ok, "detail": f"{model_for(db, agent)} answered" if ok else f"Unexpected reply: {data!r}"[:200]}
    if agent.provider == "bland":
        from app.services.ai_agent import account_status
        s = account_status(db, key=creds["api_key"])
        return {"ok": True, "detail": f"Bland balance ${s['balance']:.2f}" if s.get("balance") is not None else "Bland key works"}
    if agent.provider == "twilio_gemini":
        sid, token = creds["account_sid"], creds["api_key"]
        try:
            r = httpx.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json",
                          params={"PhoneNumber": creds["from_number"]}, auth=(sid, token), timeout=20)
        except httpx.HTTPError as exc:
            raise AgentError(f"Twilio unreachable: {exc}") from exc
        if r.status_code == 401:
            raise AgentError("Twilio rejected the Account SID / Auth Token.")
        if r.status_code >= 400:
            raise AgentError(f"Twilio error {r.status_code}: {r.text[:200]}")
        owned = r.json().get("incoming_phone_numbers") or []
        if not owned:
            raise AgentError(f"{creds['from_number']} isn't a number on this Twilio account.")
        from app.services.live_agent import status as gemini_status
        g = gemini_status(db)
        if not g["configured"]:
            raise AgentError(f"Twilio OK, but Gemini isn't available: {g['error']}")
        return {"ok": True, "detail": f"Twilio number {creds['from_number']} ready · Gemini project {g['project']}"}
    raise AgentError("Unknown provider")


def default_agent(db: Session, kind: str) -> Optional[Agent]:
    from sqlalchemy import select
    return db.execute(select(Agent).where(Agent.kind == kind, Agent.active == True)  # noqa: E712
                      .order_by(Agent.is_default.desc(), Agent.created_at).limit(1)).scalars().first()
