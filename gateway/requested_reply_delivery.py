"""In-process final-delivery receipt, bound before a requested turn starts.

This is not a replay token or a second delivery scheduler. The stream consumer
must prove a terminal transport ACK; the existing after-reply owner consumes
this receipt and still owns policy, source and publication fences.
"""
from dataclasses import dataclass, field
from typing import Any


def _scope(source):
    return (source.platform, source.chat_type, source.chat_id, source.user_id,
            getattr(source, 'thread_id', None))


@dataclass(frozen=True)
class RequestedReplyOwner:
    runner: Any = field(repr=False)
    adapter: Any = field(repr=False)
    bot: Any = field(repr=False)
    credential: Any = field(repr=False)
    scope: tuple
    session_key: str
    generation: int

    @classmethod
    def capture(cls, runner, source, session_key, generation):
        try:
            adapter = runner._adapter_for_source(source)
            return cls(runner, adapter, getattr(adapter, '_bot', None),
                       runner._adapter_credential_fingerprint(adapter),
                       _scope(source), session_key, generation)
        except Exception:
            return None  # Optional presence must not fail the requested turn.

    def current(self, runner, source, session_key, generation):
        return (runner is self.runner and session_key == self.session_key
                and generation == self.generation and generation is not None
                and _scope(source) == self.scope
                and runner._is_session_run_current(session_key, generation)
                and runner._is_user_authorized(source)
                and runner._adapter_for_source(source) is self.adapter
                and getattr(self.adapter, '_bot', None) is self.bot
                and runner._adapter_credential_fingerprint(self.adapter) == self.credential)

    def receipt(self, consumer, final_text):
        from gateway.stream_consumer import GatewayStreamConsumer
        if (isinstance(consumer, GatewayStreamConsumer) and consumer.adapter is self.adapter
                and consumer.chat_id == self.scope[2]):
            acks = consumer.confirmed_final_transport_acks(final_text)
            if acks:
                return RequestedReplyReceipt(self, final_text, acks, consumer)
        return None

    def receipt_for_edit(self, adapter, result, final_text):
        """A gateway reconciliation edit ACK covers its exact complete payload."""
        from gateway.stream_consumer import FinalTransportAck, GatewayStreamConsumer
        if adapter is not self.adapter or getattr(result, "success", False) is not True:
            return None
        ack = FinalTransportAck("edit_message", final_text,
                                tuple(GatewayStreamConsumer._message_ids_from_result(result)))
        return RequestedReplyReceipt(self, final_text, (ack,))


@dataclass(frozen=True)
class RequestedReplyReceipt:
    owner: RequestedReplyOwner = field(repr=False)
    text: str = field(repr=False)
    transport_acks: tuple = field(default=(), repr=False)
    consumer: Any = field(default=None, repr=False)

    def accepts(self, runner, event, session_key, generation, final_text):
        return (bool(self.transport_acks) and self.text == final_text
                and (self.consumer is None or self.consumer.confirmed_final_transport_acks(final_text)
                     == self.transport_acks)
                and not getattr(event, 'internal', False)
                and self.owner.current(runner, event.source, session_key, generation))
