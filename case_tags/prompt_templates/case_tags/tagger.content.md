## The case

**Title:** {{ case.title }}
{% if case.short_description %}**Summary:** {{ case.short_description }}
{% endif %}
### Description

{{ case.description|default:"(none recorded)" }}

### Key allegations
{% for allegation in case.key_allegations %}- {{ allegation }}
{% empty %}(none recorded)
{% endfor %}
Tags currently on this case, which your answer replaces: {{ current_tags|default:"(none)" }}

## The vocabulary

Pick from these before considering anything new.
{% for axis in vocabulary %}
### {{ axis.id }} — {{ axis.label_en }} (at most {{ axis.max_per_case }} per case)
{% for term in axis.terms %}- `{{ term.id }}` — {{ term.label_en }}{% if term.label_ne %} — {{ term.label_ne }}{% endif %}
{% empty %}(no terms yet on this axis)
{% endfor %}{% endfor %}
