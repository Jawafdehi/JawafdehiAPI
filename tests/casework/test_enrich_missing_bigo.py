"""Tests for the DB-free standalone BIGO enricher (casework/enrich_missing_bigo.py).

Focus on _coerce_bigo_int: CIAA writes paisa after a danda '।', slash '/', or
dot '.', and blindly stripping non-digits used to fold the paisa digits into the
rupee figure (a 10-100x inflation that reached production, e.g. 080-CR-0158).
No database and no network are touched.
"""

from casework import enrich_missing_bigo as emb


class TestCoerceBigoInt:
    def test_danda_paisa_dropped(self):
        # 080-CR-0158: २३,७५,४६,३२४।५७ must be 237546324, NOT 2375463245.
        assert emb._coerce_bigo_int("२३,७५,४६,३२४।५७") == 237546324

    def test_danda_paisa_with_currency_prefix(self):
        # 080-CR-0181
        assert emb._coerce_bigo_int("रु.२६,६३,३७,३९८।१२") == 266337398

    def test_pipe_paisa_dropped(self):
        # OCR frequently misreads the danda '।' as a vertical pipe '|'.
        assert emb._coerce_bigo_int("२३,७५,४६,३२४|५७") == 237546324

    def test_slash_paisa_dropped(self):
        assert emb._coerce_bigo_int("१,४६,८१,२२५/९०") == 14681225

    def test_trailing_slash_dash(self):
        assert emb._coerce_bigo_int("40,85,74,740/-") == 408574740

    def test_ascii_decimal_paisa_dropped(self):
        assert emb._coerce_bigo_int("237546324.57") == 237546324

    def test_clean_integer_string(self):
        assert emb._coerce_bigo_int("237546324") == 237546324

    def test_plain_int_passthrough(self):
        assert emb._coerce_bigo_int(237546324) == 237546324

    def test_float_truncates(self):
        assert emb._coerce_bigo_int(237546324.57) == 237546324

    def test_zero_is_none(self):
        assert emb._coerce_bigo_int(0) is None

    def test_none_is_none(self):
        assert emb._coerce_bigo_int(None) is None

    def test_empty_string_is_none(self):
        assert emb._coerce_bigo_int("रु.") is None
