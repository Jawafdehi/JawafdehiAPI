{% comment %}
Content block for case_proposal.intent.

`case_snapshot` and `observation` arrive ALREADY FENCED — the caller wraps them
with llm.templating.fence() before they get here, because fencing is a Python
concern (it needs a per-call nonce) and because a template has no way to know
which of its values came from outside.

The unfenced text around them is the only thing the model is meant to treat as
instruction; see _shared/untrusted_data.md, included by the system prompt.
{% endcomment %}
Here is the case as the archive currently records it.

{{ case_snapshot }}

Here is what the monitoring system observed.

{{ observation }}

Decide whether that observation warrants exactly one proposed change to the case above, or none.

Before you answer, check the case's existing timeline entry by entry. If the observation is already recorded — even in different words, even with a slightly different date — decline.

Reply with the single JSON object described in your instructions.
