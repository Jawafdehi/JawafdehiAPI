{% comment %}
A reference content template, exercised by llm/tests/test_templating.py.

It deliberately contains the three things that break a naively-configured
template engine, so the tests around it fail loudly if the prompt engine is
ever swapped for the HTML one:

  - HTML tags, which autoescaping would mangle
  - JSON with quotes and braces, which autoescaping mangles and str.format()
    chokes on entirely
  - Devanagari, which must survive as-is

Loops and conditionals are for shaping data into prose. Computation —
json.dumps, character caps, settings lookups — belongs in Python, and the
result gets passed in as context.
{% endcomment %}
CASE: {{ case_title }}

{% if excerpts %}SOURCE EXCERPTS:
{% for excerpt in excerpts %}- {{ excerpt }}
{% endfor %}{% else %}No source excerpts were available.
{% endif %}
Summarise the case in one sentence. Use <strong> for the accused party's name.

Reply EXACTLY in this JSON shape:
{"summary": "<str>", "confidence": <float 0-1>}
