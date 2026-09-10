# Licensing

This repository is licensed under the **Hippocratic License Version 3.0 (HL3)**, an [Ethical Source](https://ethicalsource.dev) license — as is every public repository under the [Jawafdehi](https://github.com/Jawafdehi) GitHub organization.

## Why Hippocratic License 3.0?

Jawafdehi.org builds open digital infrastructure to empower Nepali citizens with transparent access to information about governance, corruption, and public entities. Our software deals with sensitive data — government records, judicial proceedings, accountability cases, and personally identifiable information about public figures.

Traditional open source licenses (MIT, Apache 2.0, GPL) are based on the premise that unrestricted access to source code is an unqualified good. In practice, this means our work could be used to:

- Build surveillance systems targeting vulnerable populations
- Power disinformation campaigns
- Enable automated discrimination
- Support oppressive government actions
- Train models for unethical purposes

The Hippocratic License 3.0 ensures our software serves its intended purpose: advancing transparency, accountability, and equity. It requires licensees to comply with international human rights laws and principles, including the UN Universal Declaration of Human Rights and the UN Global Compact.

## License Terms

The full license text is in the [LICENSE](./LICENSE) file at the root of this repository.

Key provisions:

- **Human Rights Compliance**: The software may not be used for activities that violate human rights laws or principles
- **Enforcement**: Termination runs in two stages. On learning of an alleged violation, the Licensor may notify the Licensee and allow 90 days to investigate and respond; after the earlier of that response or those 90 days, the Licensor may give notice of termination and allow a further 90 days to cease use of the software
- **Indemnity**: Licensees indemnify Jawafdehi.org for non-compliance costs
- **Ethical Source**: HL3 is an Ethical Source license, not an Open Source Initiative (OSI) approved license

Because HL3 is not OSI-approved, GitHub classifies it as "Other" and some hosted services that gate a free tier on an OSI license will not recognise it.

## SPDX Identifier

Hippocratic 3.0 is **not** on the [SPDX License List](https://spdx.org/licenses/) — the list carries `Hippocratic-2.1`, and 3.0 is only a pending request. SPDX requires a `LicenseRef-` prefix for anything not on the list, so the only spec-valid identifier for this repository is:

```text
LicenseRef-Hippocratic-3.0
```

That is what `pyproject.toml` declares, and it is what source-file headers must use:

```python
# SPDX-License-Identifier: LicenseRef-Hippocratic-3.0
```

A bare `Hippocratic-3.0` is not a valid SPDX identifier and downstream SPDX tooling may reject it.

## License Compliance Verification

The [`spdx-header-check`](./.github/workflows/spdx-header-check.yml) workflow runs on every pull request targeting `main`, and on pushes to `main`. It **fails** the build if:

- the `LICENSE` file is missing, or its contents no longer match the SHA-256 digest pinned in the workflow (so the licence text cannot be reworded or truncated without a deliberate, reviewed change); or
- any of `SECURITY.md`, `CONTRIBUTING.md` or `CODE_OF_CONDUCT.md` has been deleted.

Missing or non-`LicenseRef-Hippocratic-3.0` `SPDX-License-Identifier` headers are reported as **warnings only** — most source files in this repository do not yet carry a header, so this step annotates rather than blocks.

## Questions

For licensing questions, contact: inquiry@jawafdehi.org

## References

- [Hippocratic License Website](https://firstdonoharm.dev/)
- [Ethical Source Movement](https://ethicalsource.dev)
- [UN Universal Declaration of Human Rights](https://www.un.org/en/universal-declaration-human-rights/)
- [UN Global Compact](https://www.unglobalcompact.org/what-is-gc/mission/principles)
