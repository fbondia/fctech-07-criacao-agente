"""Substitui somente a rede Gemini; Runner, agentes, tools e SQLite são reais."""

import json

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types


class ScriptedModel(BaseLlm):
    model: str = "scripted-model"

    async def generate_content_async(self, llm_request, stream=False):
        last = llm_request.contents[-1]
        responses = [p.function_response for p in last.parts or [] if p.function_response]
        if responses and all(r.name != "transfer_to_agent" for r in responses):
            text = json.dumps([r.response for r in responses], ensure_ascii=False)
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))
            return

        command = None
        for content in reversed(llm_request.contents):
            for part in content.parts or []:
                if content.role == "user" and part.text:
                    try:
                        candidate = json.loads(part.text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(candidate, dict) and "tool" in candidate:
                        command = candidate
                        break
            if command:
                break
        tool = command["tool"]
        if tool not in llm_request.tools_dict:
            # Especialista atual retorna ao principal, que transfere ao novo.
            principal = (
                "minhas_reservas" not in llm_request.tools_dict
                and "meus_visitantes" not in llm_request.tools_dict
                and "consultar_regulamento" not in llm_request.tools_dict
            )
            call = types.FunctionCall(
                name="transfer_to_agent",
                args={"agent_name": command["agente"] if principal else "aurora"},
            )
        else:
            call = types.FunctionCall(name=tool, args=command.get("args", {}))
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(function_call=call)])
        )
