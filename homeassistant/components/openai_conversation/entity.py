"""Base entity for OpenAI."""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator, Callable
import json
from mimetypes import guess_file_type
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import openai
from openai._streaming import AsyncStream
from openai.types.responses import (
    EasyInputMessageParam,
    FunctionToolParam,
    ResponseCompletedEvent,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseFunctionToolCallParam,
    ResponseIncompleteEvent,
    ResponseInputFileParam,
    ResponseInputImageParam,
    ResponseInputMessageContentListParam,
    ResponseInputParam,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseOutputMessageParam,
    ResponseReasoningItem,
    ResponseReasoningItemParam,
    ResponseStreamEvent,
    ResponseTextDeltaEvent,
    ToolParam,
    WebSearchToolParam,
)
from openai.types.responses.response_input_param import FunctionCallOutput
from openai.types.responses.web_search_tool_param import UserLocation
from voluptuous_openapi import convert

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, llm
from homeassistant.helpers.entity import Entity

from .const import (
    CONF_CHAT_MODEL,
    CONF_MAX_TOKENS,
    CONF_REASONING_EFFORT,
    CONF_TEMPERATURE,
    CONF_TOP_P,
    CONF_WEB_SEARCH,
    CONF_WEB_SEARCH_CITY,
    CONF_WEB_SEARCH_CONTEXT_SIZE,
    CONF_WEB_SEARCH_COUNTRY,
    CONF_WEB_SEARCH_REGION,
    CONF_WEB_SEARCH_TIMEZONE,
    CONF_WEB_SEARCH_USER_LOCATION,
    DOMAIN,
    LOGGER,
    RECOMMENDED_CHAT_MODEL,
    RECOMMENDED_MAX_TOKENS,
    RECOMMENDED_REASONING_EFFORT,
    RECOMMENDED_TEMPERATURE,
    RECOMMENDED_TOP_P,
    RECOMMENDED_WEB_SEARCH_CONTEXT_SIZE,
)

if TYPE_CHECKING:
    from . import OpenAIConfigEntry


# Max number of back and forth with the LLM to generate a response
MAX_TOOL_ITERATIONS = 10


def _format_tool(
    tool: llm.Tool, custom_serializer: Callable[[Any], Any] | None
) -> FunctionToolParam:
    """Format tool specification."""
    return FunctionToolParam(
        type="function",
        name=tool.name,
        parameters=convert(tool.parameters, custom_serializer=custom_serializer),
        description=tool.description,
        strict=False,
    )


def _convert_content_to_param(
    content: conversation.Content,
) -> ResponseInputParam:
    """Convert any native chat message for this agent to the native format."""
    messages: ResponseInputParam = []
    if isinstance(content, conversation.ToolResultContent):
        return [
            FunctionCallOutput(
                type="function_call_output",
                call_id=content.tool_call_id,
                output=json.dumps(content.tool_result),
            )
        ]

    if content.content:
        role: Literal["user", "assistant", "system", "developer"] = content.role
        if role == "system":
            role = "developer"
        messages.append(
            EasyInputMessageParam(type="message", role=role, content=content.content)
        )

    if isinstance(content, conversation.AssistantContent) and content.tool_calls:
        messages.extend(
            ResponseFunctionToolCallParam(
                type="function_call",
                name=tool_call.tool_name,
                arguments=json.dumps(tool_call.tool_args),
                call_id=tool_call.id,
            )
            for tool_call in content.tool_calls
        )
    return messages


async def _transform_stream(
    chat_log: conversation.ChatLog,
    result: AsyncStream[ResponseStreamEvent],
    messages: ResponseInputParam,
) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
    """Transform an OpenAI delta stream into HA format."""
    async for event in result:
        LOGGER.debug("Received event: %s", event)

        if isinstance(event, ResponseOutputItemAddedEvent):
            if isinstance(event.item, ResponseOutputMessage):
                yield {"role": event.item.role}
            elif isinstance(event.item, ResponseFunctionToolCall):
                # OpenAI has tool calls as individual events
                # while HA puts tool calls inside the assistant message.
                # We turn them into individual assistant content for HA
                # to ensure that tools are called as soon as possible.
                yield {"role": "assistant"}
                current_tool_call = event.item
        elif isinstance(event, ResponseOutputItemDoneEvent):
            item = event.item.model_dump()
            item.pop("status", None)
            if isinstance(event.item, ResponseReasoningItem):
                messages.append(cast(ResponseReasoningItemParam, item))
            elif isinstance(event.item, ResponseOutputMessage):
                messages.append(cast(ResponseOutputMessageParam, item))
            elif isinstance(event.item, ResponseFunctionToolCall):
                messages.append(cast(ResponseFunctionToolCallParam, item))
        elif isinstance(event, ResponseTextDeltaEvent):
            yield {"content": event.delta}
        elif isinstance(event, ResponseFunctionCallArgumentsDeltaEvent):
            current_tool_call.arguments += event.delta
        elif isinstance(event, ResponseFunctionCallArgumentsDoneEvent):
            current_tool_call.status = "completed"
            yield {
                "tool_calls": [
                    llm.ToolInput(
                        id=current_tool_call.call_id,
                        tool_name=current_tool_call.name,
                        tool_args=json.loads(current_tool_call.arguments),
                    )
                ]
            }
        elif isinstance(event, ResponseCompletedEvent):
            if event.response.usage is not None:
                chat_log.async_trace(
                    {
                        "stats": {
                            "input_tokens": event.response.usage.input_tokens,
                            "output_tokens": event.response.usage.output_tokens,
                        }
                    }
                )
        elif isinstance(event, ResponseIncompleteEvent):
            if event.response.usage is not None:
                chat_log.async_trace(
                    {
                        "stats": {
                            "input_tokens": event.response.usage.input_tokens,
                            "output_tokens": event.response.usage.output_tokens,
                        }
                    }
                )

            if (
                event.response.incomplete_details
                and event.response.incomplete_details.reason
            ):
                reason: str = event.response.incomplete_details.reason
            else:
                reason = "unknown reason"

            if reason == "max_output_tokens":
                reason = "max output tokens reached"
            elif reason == "content_filter":
                reason = "content filter triggered"

            raise HomeAssistantError(f"OpenAI response incomplete: {reason}")
        elif isinstance(event, ResponseFailedEvent):
            if event.response.usage is not None:
                chat_log.async_trace(
                    {
                        "stats": {
                            "input_tokens": event.response.usage.input_tokens,
                            "output_tokens": event.response.usage.output_tokens,
                        }
                    }
                )
            reason = "unknown reason"
            if event.response.error is not None:
                reason = event.response.error.message
            raise HomeAssistantError(f"OpenAI response failed: {reason}")
        elif isinstance(event, ResponseErrorEvent):
            raise HomeAssistantError(f"OpenAI response error: {event.message}")


class OpenAIBaseLLMEntity(Entity):
    """OpenAI conversation agent."""

    def __init__(self, entry: OpenAIConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the entity."""
        self.entry = entry
        self.subentry = subentry
        self._attr_name = subentry.title
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="OpenAI",
            model=subentry.data.get(CONF_CHAT_MODEL, RECOMMENDED_CHAT_MODEL),
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    async def _async_handle_chat_log(
        self,
        chat_log: conversation.ChatLog,
    ) -> None:
        """Generate an answer for the chat log."""
        options = self.subentry.data

        tools: list[ToolParam] | None = None
        if chat_log.llm_api:
            tools = [
                _format_tool(tool, chat_log.llm_api.custom_serializer)
                for tool in chat_log.llm_api.tools
            ]

        if options.get(CONF_WEB_SEARCH):
            web_search = WebSearchToolParam(
                type="web_search_preview",
                search_context_size=options.get(
                    CONF_WEB_SEARCH_CONTEXT_SIZE, RECOMMENDED_WEB_SEARCH_CONTEXT_SIZE
                ),
            )
            if options.get(CONF_WEB_SEARCH_USER_LOCATION):
                web_search["user_location"] = UserLocation(
                    type="approximate",
                    city=options.get(CONF_WEB_SEARCH_CITY, ""),
                    region=options.get(CONF_WEB_SEARCH_REGION, ""),
                    country=options.get(CONF_WEB_SEARCH_COUNTRY, ""),
                    timezone=options.get(CONF_WEB_SEARCH_TIMEZONE, ""),
                )
            if tools is None:
                tools = []
            tools.append(web_search)

        model = options.get(CONF_CHAT_MODEL, RECOMMENDED_CHAT_MODEL)
        messages = [
            m
            for content in chat_log.content
            for m in _convert_content_to_param(content)
        ]

        client = self.entry.runtime_data

        # To prevent infinite loops, we limit the number of iterations
        for _iteration in range(MAX_TOOL_ITERATIONS):
            model_args = {
                "model": model,
                "input": messages,
                "max_output_tokens": options.get(
                    CONF_MAX_TOKENS, RECOMMENDED_MAX_TOKENS
                ),
                "top_p": options.get(CONF_TOP_P, RECOMMENDED_TOP_P),
                "temperature": options.get(CONF_TEMPERATURE, RECOMMENDED_TEMPERATURE),
                "user": chat_log.conversation_id,
                "stream": True,
            }
            if tools:
                model_args["tools"] = tools

            if model.startswith("o"):
                model_args["reasoning"] = {
                    "effort": options.get(
                        CONF_REASONING_EFFORT, RECOMMENDED_REASONING_EFFORT
                    )
                }
            else:
                model_args["store"] = False

            try:
                result = await client.responses.create(**model_args)
            except openai.RateLimitError as err:
                LOGGER.error("Rate limited by OpenAI: %s", err)
                raise HomeAssistantError("Rate limited or insufficient funds") from err
            except openai.OpenAIError as err:
                LOGGER.error("Error talking to OpenAI: %s", err)
                raise HomeAssistantError("Error talking to OpenAI") from err

            async for content in chat_log.async_add_delta_content_stream(
                self.entity_id, _transform_stream(chat_log, result, messages)
            ):
                if not isinstance(content, conversation.AssistantContent):
                    messages.extend(_convert_content_to_param(content))

            if not chat_log.unresponded_tool_results:
                break


async def async_prepare_files_for_prompt(
    hass: HomeAssistant, files: list[Path]
) -> ResponseInputMessageContentListParam:
    """Append files to a prompt.

    Caller needs to ensure that the files are allowed.
    """

    def append_files_to_content() -> ResponseInputMessageContentListParam:
        content: ResponseInputMessageContentListParam = []

        for file_path in files:
            if not file_path.exists():
                raise HomeAssistantError(f"`{file_path}` does not exist")

            mime_type, _ = guess_file_type(file_path)

            if not mime_type or not mime_type.startswith(("image/", "application/pdf")):
                raise HomeAssistantError(
                    "Only images and PDF are supported by the OpenAI API,"
                    f"`{file_path}` is not an image file or PDF"
                )

            base64_file = base64.b64encode(file_path.read_bytes()).decode("utf-8")

            if mime_type.startswith("image/"):
                content.append(
                    ResponseInputImageParam(
                        type="input_image",
                        image_url=f"data:{mime_type};base64,{base64_file}",
                        detail="auto",
                    )
                )
            elif mime_type.startswith("application/pdf"):
                content.append(
                    ResponseInputFileParam(
                        type="input_file",
                        filename=str(file_path),
                        file_data=f"data:{mime_type};base64,{base64_file}",
                    )
                )

        return content

    return await hass.async_add_executor_job(append_files_to_content)
