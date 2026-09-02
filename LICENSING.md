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
- **Enforcement**: Licensor may terminate the license for human rights violations after a 90-day notice and cure period
- **Indemnity**: Licensees indemnify Jawafdehi.org for non-compliance costs
- **Ethical Source**: HL3 is an Ethical Source license, not an Open Source Initiative (OSI) approved license

Because HL3 is not OSI-approved, GitHub classifies it as "Other" and some hosted services that gate a free tier on an OSI license will not recognise it.

## License Compliance Verification

The [`spdx-header-check`](./.github/workflows/spdx-header-check.yml) workflow runs on every pull request. It **fails** the build if the `LICENSE` file is missing or is not HL3, and reports missing or non-HL3 `SPDX-License-Identifier` headers as warnings.

## Questions

For licensing questions, contact: inquiry@jawafdehi.org

## References

- [Hippocratic License Website](https://firstdonoharm.dev/)
- [Ethical Source Movement](https://ethicalsource.dev)
- [UN Universal Declaration of Human Rights](https://www.un.org/en/universal-declaration-human-rights/)
- [UN Global Compact](https://www.unglobalcompact.org/what-is-gc/mission/principles)
