# Custom OpenSearch image with the analysis-icu plugin baked in.
#
# The bilingual Nepali analyzers (icu_normalizer / icu_tokenizer / icu_transform
# / icu_folding — see shared/jawafdehi_shared/search/mappings.py) require the
# analysis-icu plugin, which is NOT bundled in the stock image. Installing it at
# container BOOT (the previous compose `command:` override) costs a network fetch
# on every start and fails closed if the plugin mirror is unreachable. Baking it
# into the image makes startup deterministic and offline-friendly, and is the
# documented production approach.
#
# Pin the base tag to match the version the platform was validated against. Bump
# this in lockstep with the cluster you deploy.
ARG OPENSEARCH_VERSION=2.13.0
FROM opensearchproject/opensearch:${OPENSEARCH_VERSION}

# analysis-icu is Apache-2.0 (free). --batch accepts the security prompt. The
# plugin version is resolved from the running OpenSearch version automatically.
RUN /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-icu

# Fail the build (not silently ship a broken image) if the plugin is absent.
RUN /usr/share/opensearch/bin/opensearch-plugin list | grep -q analysis-icu
