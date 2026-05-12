from rest_framework import serializers


class NGMQuerySerializer(serializers.Serializer):
    query = serializers.CharField(max_length=2048)
    timeout = serializers.FloatField(
        required=False, min_value=1, max_value=15, default=15
    )


class CourtCaseEntitySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    case_number = serializers.CharField()
    court_identifier = serializers.CharField()
    side = serializers.CharField()
    name = serializers.CharField()
    address = serializers.CharField(allow_null=True)
    nes_id = serializers.CharField(allow_null=True)


class CourtCaseHearingSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    case_number = serializers.CharField()
    court_identifier = serializers.CharField()
    hearing_date_bs = serializers.CharField(allow_null=True)
    hearing_date_ad = serializers.DateField(allow_null=True)
    bench = serializers.CharField(allow_null=True)
    bench_type = serializers.CharField(allow_null=True)
    judge_names = serializers.CharField(allow_null=True)
    lawyer_names = serializers.CharField(allow_null=True)
    serial_no = serializers.CharField(allow_null=True)
    case_status = serializers.CharField(allow_null=True)
    decision_type = serializers.CharField(allow_null=True)
    remarks = serializers.CharField(allow_null=True)


class CourtCaseDetailSerializer(serializers.Serializer):
    case_number = serializers.CharField()
    court_identifier = serializers.CharField()
    registration_date_bs = serializers.CharField(allow_null=True)
    registration_date_ad = serializers.DateField(allow_null=True)
    case_type = serializers.CharField(allow_null=True)
    division = serializers.CharField(allow_null=True)
    category = serializers.CharField(allow_null=True)
    section = serializers.CharField(allow_null=True)
    plaintiff = serializers.CharField(allow_null=True)
    defendant = serializers.CharField(allow_null=True)
    original_case_number = serializers.CharField(allow_null=True)
    case_id = serializers.CharField(allow_null=True)
    priority = serializers.CharField(allow_null=True)
    registration_number = serializers.CharField(allow_null=True)
    case_status = serializers.CharField(allow_null=True)
    verdict_date_bs = serializers.CharField(allow_null=True)
    verdict_date_ad = serializers.DateField(allow_null=True)
    verdict_judge = serializers.CharField(allow_null=True)
    status = serializers.CharField(allow_null=True)
    hearings = CourtCaseHearingSerializer(many=True)
    entities = CourtCaseEntitySerializer(many=True)
