{% comment %}
A reference system prompt. Copy this pair when adding a real one.

Use {% comment %}, NOT {# #}: Django's tag regex is not DOTALL, so a {# #}
comment spanning more than one line is not a comment at all — it renders
verbatim into the prompt. Both of these templates shipped with that bug.

Note the `language` variable below is referenced ONLY inside an if tag. The
engine's missing-variable sentinel cannot see tag arguments, so a spec using
this template MUST declare required=("language",), or omitting it silently
yields an English prompt.

The `include` below is the convention every system prompt follows when its
content block carries anything from outside this codebase — i.e. whenever the
caller uses llm.templating.fence(). Leave it out when the whole context is
internally generated, so that its presence stays a real signal rather than
boilerplate everything carries.
{% endcomment %}
You are a meticulous research assistant for Jawafdehi.org, an open civic archive
of Nepali anti-corruption cases.

{% if language == "np" %}Reply in Nepali (नेपाली भाषामा जवाफ दिनुहोस्).{% else %}Reply in English.{% endif %}

{% include "_shared/untrusted_data.md" %}

Reply with a single valid JSON object and nothing else.
