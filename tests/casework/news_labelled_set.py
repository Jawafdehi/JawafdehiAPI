"""Hand-labelled (case, candidate article) pairs -- the news verifier's
false-positive gate.

WHY A LABELLED SET AT ALL. A wrong news bind publicly ties named people to
a corruption case they may have nothing to do with, so the acceptance bar
is not a metric to optimise -- it is an assertion. `test_enrich_news_
articles.py` requires ZERO of the `no_match` rows below to reach a bind.

PROVENANCE. Rows are real production data, read 2026-08-05 from the 33 news
evidence entries on the 15 IN_REVIEW cases (`GET /api/cases/{slug}/`). Each
`article.text` is the transcript of the bound material, trimmed to the 900
chars the verifier actually feeds. Labels are this session's own reading of
each article against each case -- NOT the prod bind, which is wrong on two
rows (see below).

A NEGATIVE IS BUILT BY CROSSING TWO REAL ROWS. Pairing case A with case B's
article yields a negative whose article is genuine reporting about a genuine
case -- the shape a search actually returns. `article_from` records which
case the article really belongs to.

TWO ROWS ARE LIVE PRODUCTION MIS-BINDS. `vikal-paudel-080-cr-0174-illegal-
assets` carries three news entries; only one is about it. The other two are
the same accused's OTHER two corruption cases, and one prod note says so in
as many words ("सोही आरोपी ... अर्को भ्रष्टाचार मुद्दा"). They are labelled
`no_match` here on the brief's rule -- a different case involving the same
person is not evidence for this one -- which means this fixture deliberately
contradicts prod. See `findings.md`.

ONE ROW IS SYNTHETIC and marked so in its `why`: no real "same case number,
different court" pair exists, because every case in the batch is Special
Court.
"""

LABELLED_PAIRS = [
    {
        "case": {
            "slug": "case-080-cr-0032-toran-karki-illegal-assets",
            "court_case_no": "080-CR-0032",
            "title": (
                "जलस्रोत अनुसन्धान विकास केन्द्रका लेखा अधिकृत तोरण बहादुर कार्कीले रु.४.६७ "
                "करोड स्रोत नखुलेको सम्पत्ति आर्जन गरेको आरोप (080-CR-0032)"
                ),
            "short_description": (
                "जलस्रोत अनुसन्धान विकास केन्द्र, पुल्चोकका लेखा अधिकृत तोरण बहादुर कार्कीले "
                "वैध आयभन्दा बढी खर्च-लगानी गरी करिब रु.४.६७ करोड स्रोत नखुलेको सम्पत्ति "
                "आर्जन गरी श्रीमतीसमेतको नाममा लुकाएको आरोप।"
                ),
            "key_allegations": [
                (
                    "जलस्रोत अनुसन्धान विकास केन्द्र, पुल्चोकका लेखा अधिकृत तोरण बहादुर "
                    "कार्कीले सार्वजनिक सेवा प्रवेश गरेको मिति २०५३।०२।१७ देखि २०७९।०८।२८ "
                    "सम्मको जाँच अवधिमा वैध आय रु. १ करोड ८ लाखभन्दा बढी हुँदा पनि घर "
                    "निर्माण, जग्गा खरिद, शेयर लगानी, सवारी साधन खरिद र बैंक मौज्दात लगायतमा "
                    "रु. ५ करोड ७५ लाखभन्दा बढी खर्च तथा लगानी गरी करिब रु. ४ करोड ६७ लाख "
                    "स्रोत नखुलेको सम्पत्ति आर्जन गरेको।"
                    ),
                (
                    "तोरण बहादुर कार्कीले वैध आयसँग नमिल्ने खर्च तथा लगानीबाट आफू र परिवारको "
                    "अमिल्दो तथा अस्वाभाविक उच्चस्तरको जीवनयापन कायम गरेको।"
                    ),
                (
                    "तोरण बहादुर कार्कीले स्रोत नखुलेको सम्पत्ति श्रीमती उर्मिला थापा "
                    "कार्कीसमेतको नाममा जग्गा, शेयर, सवारी साधन र बैंक मौज्दातमा राखी "
                    "सम्पत्ति लुकाउने वा स्थानान्तरण गर्ने काम गरेको।"
                    ),
            ],
            "accused": [
                "Toran Bahadur Karki",
                "Urmila Thapa Karki",
            ],
        },
        "article": {
            "url": (
                "https://thehimalayantimes.com/nepal/government-official-held-for-accumulating-illegal-property-worth-rs-46mn"
                ),
            "title": (
                "Government official held for accumulating illegal property worth Rs 46mn - "
                "The Himalayan Times - Nepal's No.1 English Daily Newspaper | Nepal News, "
                "Latest Politics, Business, World, Sports, Entertainment, Travel, Life Style "
                "News"
                ),
            "text": (
                "**KATHMANDU, SEPTEMBER 9** The Commission for the Investigation of Abuse of "
                "Authority has filed a chargesheet at the Special Court against Toran Bahadur "
                "Karki, account officer of Lalitpur-based Water Resources Research "
                "Development Centre, for allegedly amassing disproportionate assets worth "
                "around Rs 46 million. The CIAA had launched a thorough investigation into "
                "his property status and income source after it received a complaint that he "
                "had accummulated illegal assets. The anti-graft body said Karki managed to "
                "establish the income source of only Rs 11 million of the assets, both "
                "moveable and immovable, worth around Rs 57.5 million he had claimed to have "
                "earned after joining government office on 30 May 1996. A press release "
                "issued by the CIAA claimed that Karki was found to have accumulated illegal "
                "assets of around Rs 46 million through corruption and financial "
                "irregularities till last f"
                ),
            "published": None,
        },
        "label": "match",
        "why": (
            "filing: names Toran Bahadur Karki, the Water Resources Research Development "
            "Centre post and Rs 46m -- the case's own Rs 4.67 crore."
            ),
        "article_from": "080-CR-0032",
    },
    {
        "case": {
            "slug": "case-080-cr-0106-sanjay-sharma-procurement-fraud",
            "court_case_no": "080-CR-0106",
            "title": (
                "मेलम्ची खानेपानी आयोजना: CMC di Ravenna को ठेक्कामा अतिरिक्त पेश्की, बिल "
                "कट्टी नगरी र भुक्तानी अनियमितताबाट रु. ८९ करोड हानि, सचिव डा. संजय शर्मा "
                "लगायत विरुद्ध भ्रष्टाचार मुद्दा (080-CR-0106)"
                ),
            "short_description": (
                "मेलम्ची खानेपानी आयोजनाको रु. ७ अर्ब ७२ करोडको ठेक्कामा कानुनविपरीत MOU "
                "मार्फत अतिरिक्त पेश्की दिई, ब्याज, रकम र Rental Charge असुल/कट्टी नगरी "
                "सरकारलाई करिब रु. ८९ करोड हानि पुर्‍याएको आरोप।"
                ),
            "key_allegations": [
                (
                    "मेलम्ची खानेपानी विकास समितिका तत्कालीन बोर्ड अध्यक्ष, कार्यकारी "
                    "निर्देशक, लेखा तथा प्राविधिक पदाधिकारीहरूले मेलम्ची खानेपानी आयोजनाको "
                    "रु. ७ अर्ब ७२ करोडको ठेक्कामा सम्झौता र सार्वजनिक खरिद कानुनमा व्यवस्था "
                    "नभएको MOU खडा गरी निर्माण व्यवसायीलाई रु. ८५ करोडभन्दा बढी अतिरिक्त "
                    "मोबिलाइजेसन तथा भ्यालु इन्जिनियरिङ पेश्की दिई सोको ब्याजसमेत असुल नगरी "
                    "२०७०।०३।३१ देखि २०७५।०८।२९ सम्म सरकारी सम्पत्तिमा हानि पुर्‍याएका।"
                    ),
                (
                    "परामर्शदाता र व्यवस्थापन पक्षले रनिङ्ग बिल IPC 39 मार्फत दिइएको रु. १० "
                    "करोड Provisional Payment पछिल्ला बिलबाट समायोजन वा कट्टी नगरी निर्माण "
                    "व्यवसायीलाई अनुचित लाभ पुर्‍याएका।"
                    ),
                (
                    "परामर्शदाता, व्यवस्थापन पक्ष र निर्माण व्यवसायीको संलग्नतामा अघिल्लो "
                    "ठेक्काबाट जफत घर, निर्माण सामग्री, मेसिन तथा उपकरण प्रयोग गरेबापतको करिब "
                    "रु. २७ करोड ६६ लाख Rental Charge IPC बिलबाट कट्टी नगरी सरकारी रकम "
                    "नोक्सान गराएका।"
                    ),
            ],
            "accused": [
                "Shiv Kumar Sharma",
                "Bhojvikram Thapa",
                "Kedar Prasad Aryal",
                "Beganath Poudel",
                "Ramchandra Nyaupane",
                "Manibhadra Nyaupane",
                "Suryaraj Kadel",
                "Ramchandra Devkota",
            ],
        },
        "article": {
            "url": "https://english.ratopati.com/story/33976",
            "title": (
                "Final hearing begins in Melamchi Water Project corruption case | Ratopati | "
                "No.1 Nepali News Portal"
                ),
            "text": (
                "# Final hearing begins in Melamchi Water Project corruption case Kathmandu, "
                "August 26 — The final hearing has commenced in the case filed by the "
                "Commission for Investigation of Abuse of Authority (CIAA), which alleges "
                "corruption in the national pride Melamchi drinking water project. The bench, "
                "comprising Special Court Chairman Teknarayan Kunwar, Tejnarayan Singh Rai, "
                "and Murari Babu Shrestha, began the final hearing on Sunday. The CIAA filed "
                "a corruption case against 14 individuals on February 18, 2024, including "
                "three former secretaries, as well as consultants and construction companies, "
                "accusing them of financial irregularities due to repeatedly extending the "
                "project's deadline. The indictment claims that payments were improperly made "
                "in the project and that the accused were involved in this process. It "
                "alleges that construction professionals were paid without performing any "
                "work and t"
                ),
            "published": None,
        },
        "label": "match",
        "why": "hearing: final hearing in the Melamchi Drinking Water Project case.",
        "article_from": "080-CR-0106",
    },
    {
        "case": {
            "slug": "case-080-cr-0141-digambar-jha-procurement-fraud",
            "court_case_no": "080-CR-0141",
            "title": (
                "नेपाल दूरसञ्चार प्राधिकरणको MDMS खरिदमा Nuemera JV लाई ९१ करोड ९८ लाखको "
                "गैरकानूनी लाभ पुर्‍याउने तत्कालीन अध्यक्ष दिगम्बर झा लगायत विरुद्ध "
                "भ्रष्टाचार मुद्दा (080-CR-0141)"
                ),
            "short_description": (
                "नेपाल दूरसञ्चार प्राधिकरणका पदाधिकारीहरूले अनिवार्य खरिदपूर्व प्रक्रिया नगरी "
                "MDMS खरिदमा Nuemera JV लाई फाइदा पुर्‍याउँदै प्राधिकरण तथा सरकारलाई रु. ९१ "
                "करोड ९८ लाखभन्दा बढी हानि पुर्‍याएको आरोप।"
                ),
            "key_allegations": [
                (
                    "नेपाल दूरसञ्चार प्राधिकरणका तत्कालीन अध्यक्षहरू दिगम्बर झा र पुरुषोत्तम "
                    "प्रसाद खनालसहितका पदाधिकारीहरूले MDMS खरिदमा पूर्वअध्ययन, खरिद "
                    "गुरुयोजना, लागत अनुमानको आधार, कार्यस्थल यकिन र वस्तु तथा परामर्श सेवाको "
                    "अलग मूल्याङ्कन नगरी अस्वाभाविक रूपमा बढी काम र लागत देखाएर Nuemera JV "
                    "लाई लाभ पुर्‍याई प्राधिकरण तथा नेपाल सरकारलाई रु. ९१ करोड ९८ लाखभन्दा "
                    "बढी हानिनोक्सानी पुर्‍याएको।"
                    ),
                (
                    "प्राधिकरणका खरिद एकाइका तत्कालीन पदाधिकारीहरूले प्राविधिक मूल्याङ्कन "
                    "प्रतिवेदन, आर्थिक प्रस्ताव, सम्झौता र मूल्यसम्बन्धी निर्णयलगायतका "
                    "सार्वजनिक कागजातहरू काम सम्पन्न नहुँदै लुकाउने वा नष्ट गर्ने काम गरेको।"
                    ),
                (
                    "मूल्याङ्कन समितिका सदस्यहरूले Nuemera JV को सर्तसहितको प्राविधिक "
                    "प्रस्तावमा आफ्नै सर्त पूरा नगराई आर्थिक प्रस्ताव खोल्न र सम्झौता गर्न "
                    "सिफारिस गरी ToR विपरीतका सर्त अन्देखा गरेर गलत मूल्याङ्कन प्रतिवेदन तयार "
                    "गरेको।"
                    ),
            ],
            "accused": [
                "Digambar Jha",
                "Purushottam Prasad Khanal",
                "Anandraj Khanal",
                "Dipesh Acharya",
                "Surendra Lal Hada",
                "Achyutnand Mishra",
                "Min Prasad Aryal",
                "Revatiram Pant",
            ],
        },
        "article": {
            "url": (
                "https://myrepublica.nagariknetwork.com/news/former-nta-chairs-jha-khanal-convicted-in-mdms-procurement-embezzlement-19-80.html"
                ),
            "title": (
                "Former NTA Chairs Jha, Khanal convicted in MDMS procurement embezzlement - "
                "myRepublica - The New York Times Partner, Latest news of Nepal in English, "
                "Latest News Articles | Republica"
                ),
            "text": (
                "**Other employees acquitted ** KATHMANDU, March 7: The Special Court "
                "convicted two former chairpersons of the Nepal Telecommunications Authority "
                "(NTA) in a corruption case related to the procurement of the Mobile Device "
                "Management System (MDMS) but acquitted the other accused employees. On "
                "Thursday, a joint bench of Judges Tek Narayan Kunwar, Ritesh Thapa, and "
                "Bidur Koirala found former chairpersons Digambar Jha and Purushottam Prasad "
                "Khanal guilty in the embezzlement case. The court ruled that they committed "
                "offenses under Section 8, Subsection (1), Clauses (a), (d), and (j) of the "
                "Corruption Prevention Act, 2002. Additionally, the court convicted the "
                "company that secured the contract. ### MDMS to be fully implemented from "
                "today, entrepreneurs expect a... The Special Court ordered a one-year prison "
                "sentence and a fine of Rs 58.15 million each for the former chairpersons, "
                "matching the em"
                ),
            "published": None,
        },
        "label": "match",
        "why": "verdict: two former NTA chairs convicted over MDMS procurement.",
        "article_from": "080-CR-0141",
    },
    {
        "case": {
            "slug": "case-080-cr-0070-hira-bahadur-gurung-embezzlement",
            "court_case_no": "080-CR-0070",
            "title": (
                "चितवन निकुञ्जको कोर एरियामा कंक्रिट मचानको अनुमति लिई १८ कोठे ५ तल्ले "
                "लज–होटल निर्माण: रु. १.१९ करोड बिगोमा अध्यक्ष हिरा बहादुर गुरुङ लगायत "
                "विरुद्ध भ्रष्टाचार मुद्दा (080-CR-0070)"
                ),
            "short_description": (
                "चितवन राष्ट्रिय निकुञ्जको कोर एरियामा कंक्रिट मचानको अनुमति लिई अनुमतिविपरीत "
                "१८ कोठे ५ तल्ले लज–होटल स्वरूपको भवन बनाई रु. १.१९ करोड सार्वजनिक सम्पत्ति "
                "हानिनोक्सानी गरेको आरोप।"
                ),
            "key_allegations": [
                (
                    "कुमरोज मध्यवर्ती सामुदायिक वन उपभोक्ता समूह र कंक्रिट मचान निर्माण "
                    "समितिका पदाधिकारीहरूले खैरहनी नगरपालिकाबाट विनियोजित बजेट र समूहको "
                    "आन्तरिक कोष प्रयोग गरी चितवन राष्ट्रिय निकुञ्जको कोर एरियामा कंक्रिट "
                    "मचान बनाउने अनुमति विपरीत १८ कोठे ५ तल्ले लज–होटल स्वरूपको भवन निर्माण "
                    "गरी सार्वजनिक लगानी औचित्यहीन बनाएको र तत्कालीन अध्यक्ष हिरा बहादुर "
                    "गुरुङतर्फ रु. १ करोड १९ लाखभन्दा बढी बिगो कायम भएको।"
                    ),
                (
                    "खैरहनी नगरपालिकाका प्रमुख प्रशासकीय अधिकृत पुरुषोत्तम शर्मा, इन्जिनियर र "
                    "सवइन्जिनियरले निकुञ्जसँग समन्वय नगरी र क्षेत्रको अवस्था यकिन नगरी अनुमति "
                    "लिनुअघि नै उपभोक्ता समितिसँग सम्झौता, सर्वे, नक्सा र लागत अनुमान स्वीकृत "
                    "गरी निर्माण अघि बढाएको।"
                    ),
                (
                    "चितवन राष्ट्रिय निकुञ्ज सौराहा सेक्टरका तत्कालीन सहायक संरक्षण अधिकृत "
                    "अभिनय पाठकले दिएको अनुमति र सर्तविपरीत कोर एरियामा विशाल भवन निर्माण "
                    "हुँदा समयमै अनुगमन गरी रोक्का नगर्दा रु. ३१ लाखभन्दा बढी सार्वजनिक "
                    "हानिनोक्सानी भएको।"
                    ),
            ],
            "accused": [
                "Abhinay Pathak",
                "Purushottam Sharma",
                "Rajesh Koirala",
                "Suman Shrestha",
                "Hira Bahadur Gurung",
                "Kalyan Prasad Latowla",
                "Sita Dahal (Thanda Kumari Parajuli Dahal)",
                "Ujjal Bartoula",
            ],
        },
        "article": {
            "url": "https://english.khabarhub.com/2025/14/457912/",
            "title": "CIAA appeals Special Court verdicts at Supreme Court &laquo; Khabarhub",
            "text": (
                "KATHMANDU: Dissatisfied with the rulings of the Special Court, the "
                "Commission for the Investigation of Abuse of Authority (CIAA) has filed "
                "appeals at the Supreme Court on Sunday in two separate corruption cases. One "
                "of the appeals involves former Assistant Conservation Officer Abhinay Pathak "
                "of the Sauraha Sector under Chitwan National Park. The Special Court had "
                "acquitted Pathak on Ashad 18 despite the CIAA’s claim of corruption "
                "amounting to Rs 3,159,735. Challenging the acquittal, the anti-graft body "
                "has now taken the matter to the apex court. In a separate case, the CIAA has "
                "also appealed against a Special Court decision dated February 29, 2024 "
                "involving three officials from the Survey Office in Rupandehi — Survey "
                "Officers Dhruvraj Marasini and Bindeshwar Yadav, and Surveyor Thakur Prasad "
                "Gura. The commission alleges that the officials prepared an incorrect map "
                "and unlawfully divided "
                ),
            "published": None,
        },
        "label": "match",
        "why": (
            "appeal: CIAA appeals to the Supreme Court naming Abhinay Pathak of Chitwan "
            "National Park, an accused on this case."
            ),
        "article_from": "080-CR-0070",
    },
    {
        "case": {
            "slug": "case-080-cr-0145-jeevan-shahi-procurement-fraud",
            "court_case_no": "080-CR-0145",
            "title": (
                "नेपाल वायुसेवा निगम A330-200 वाइडबडी विमान खरिदमा Price Escalation र MTOW "
                "घटाएर रु. १.४७ अर्ब हानि: मन्त्री जीवनबहादुर शाही र महाप्रबन्धक सुगतरत्न "
                "कंसाकार लगायत विरुद्ध भ्रष्टाचार मुद्दा (080-CR-0145)"
                ),
            "short_description": (
                "नेपाल वायुसेवा निगमको A330-200 वाइडबडी विमान खरिदमा कानूनविपरीत Price "
                "Escalation राखी र MTOW २४२ टनबाट २३० टनमा घटाएर पनि २४२ टनकै मूल्य भुक्तानी "
                "गराई निगमलाई करिब रु. १.४७ अर्ब हानि पुर्‍याएको आरोप।"
                ),
            "key_allegations": [
                (
                    "तत्कालीन पर्यटनमन्त्री जीवनबहादुर शाही, नेपाल वायुसेवा निगमका तत्कालीन "
                    "कार्यकारी प्रमुख सुगतरत्न कंसाकार, सञ्चालक समिति र खरिद/मूल्याङ्कन "
                    "उपसमितिका पदाधिकारीहरूले A330-200 विमान खरिदमा खरिद कानूनविपरीतको MoU, "
                    "RFP र सम्झौता स्वीकार/स्वीकृत गराई Price Escalation राख्ने र विमानको "
                    "MTOW २४२ टनबाट २३० टनमा घटाएर पनि २४२ टनकै मूल्य भुक्तानी गर्ने व्यवस्था "
                    "मिलाई नेपाल वायुसेवा निगमलाई करिब रु. १ अर्ब ४७ करोड १० लाख हानि "
                    "पुर्‍याएको।"
                    ),
                (
                    "तत्कालीन मन्त्री जीवनबहादुर शाहीले कानूनविपरीत Price Escalation र MTOW "
                    "घटाउने प्रावधान सच्याउन निर्देशन नदिई बैंक जमानत/सुरक्षणबिना Commitment "
                    "Fee भुक्तानीका लागि डलर सटही सुविधा सिफारिस गरी निगमलाई दोहोरो जोखिममा "
                    "पारेको।"
                    ),
                (
                    "विमान आपूर्तिकर्ता कन्सोर्टियम, Special Purpose Company र Escrow Agent "
                    "का प्रतिनिधिहरूले निगमका पदाधिकारीसँग मिलेमतो गरी Price Escalation "
                    "सहितको Conditional प्रस्ताव पेश गराएर डेलिभरी तालिका पछाडि धकेल्ने, MTOW "
                    "२३० टनको विमान आपूर्ति गरेर २४२ टनकै रकम लिने र करिब रु. १ अर्ब ४७ करोड "
                    "१० लाख बढी भुक्तानी प्राप्त गर्ने काम गरेको।"
                    ),
            ],
            "accused": [
                "Jiban Bahadur Shahi",
                "Sugatratna Kanskar",
                "Shankar Prasad Adhikari",
                "Shishir Kumar Dhungana",
                "Budhisagar Lamichane",
                "Teknath Acharya",
                "Muktiram Pande",
                "Jivan Prakash Sitaula",
            ],
        },
        "article": {
            "url": (
                "https://english.onlinekhabar.com/nac-wide-body-controversy-rs-1-47-billion-corruption-in-wide-body-32-defendants-including-ex-minister-shahi.html"
                ),
            "title": (
                "NAC wide-body controversy: Rs 1.47 billion corruption in wide-body, 32 "
                "defendants including ex-minister Shahi - OnlineKhabar English News"
                ),
            "text": (
                "The Commission for Investigation of Abuse of Authority (CIAA) on Thursday "
                "lodged a charge sheet against 32 persons, including former Minister for "
                "Culture, Tourism and Civil Aviation Jeevan Bahadur Shahi, on the charge of "
                "corruption in the procurement of wide-body aircraft for the national flag "
                "carrier- Nepal Airlines Corporation (NAC). Other officials facing the charge "
                "sheet include then General Manager of NAC Sugat Ratna Kansakar, the "
                "government secretary and Chairperson of NAC Board of Directors Shankar "
                "Prasad Adhikari, then Director General of Customs Department and then NAC "
                "Board of Directors Shishir Kumar Dhungana, Civil Aviation Ministry’s Joint "
                "Secretary Buddhi Sagar Lamichhane and others. The CIAA has sought Rs 1.471 "
                "million in recovery for their alleged involvement in the misappropriation in "
                "the procurement of the wide-body aircraft. Similarly, others implicated in "
                "the case are "
                ),
            "published": None,
        },
        "label": "match",
        "why": (
            "filing: 32 defendants including ex-minister Shahi over the NAC wide-body "
            "purchase; the case's own headline facts."
            ),
        "article_from": "080-CR-0145",
    },
    {
        "case": {
            "slug": "gajendra-maharjan-080-cr-0185-revenue-fy2079",
            "court_case_no": "080-CR-0185",
            "title": (
                "गोदावरी निकासी कर ठेक्का (आ.व. २०७९/०८०): नगरप्रमुख गजेन्द्र महर्जन समेत "
                "विरुद्ध रु. २६.६३ करोड राजस्व चुहावट भ्रष्टाचार मुद्दा (080-CR-0185)"
                ),
            "short_description": (
                "गोदावरी नगरपालिकाका तत्कालीन नगरप्रमुख, नगर उपप्रमुख, प्रमुख प्रशासकीय "
                "अधिकृत र राजस्व शाखा प्रमुख लगायतका पदाधिकारी तथा ठेकेदार कम्पनी र "
                "खानी/क्रसर उद्योग व्यवसायीहरू विरुद्ध आर्थिक वर्ष २०७९/०८० मा ढुङ्गा, "
                "गिट्टी, ग्राभेल, रोडा, बालुवा र माटोको निकासी शुल्क दर बागमती प्रदेश सरकारले "
                "निर्धारण गरेको दरभन्दा घटी तोकी राजस्व चुहावट गराएको आरोपमा अख्तियार "
                "दुरुपयोग अनुसन्धान आयोगले भ्रष्टाचारसम्बन्धी अभियोग दायर गरेको थियो। यसैसाथ "
                "केही पदाधिकारी प्रतिवादीहरू विरुद्ध सरकारी कागजात लुकाएको/नष्ट गरेको आरोप "
                "समेत लगाइएको थियो। प्रतिवादीहरूविरुद्ध भ्रष्टाचार निवारण ऐन, २०५९ को दफा "
                "७(ख), दफा २२ र दफा १२ बमोजिम कसुरमा अभियोग दायर गरिएको थियो। विशेष अदालतले "
                "निकासी शुल्कको दर तोक्ने अधिकार स्थानीय तहमै रहेको र प्रदेश ऐनसँग बाझिएको "
                "भन्ने विवादको निरूपण गर्ने क्षेत्राधिकार सर्वोच्च अदालतको संवैधानिक इजलासमा "
                "रहेको भन्दै, प्रतिवादीहरूले नगरसभाबाट पारित कानून कार्यान्वयन गरेकोसम्म "
                "देखिई बदनियत पुष्टि हुन नसकेको आधारमा सबै प्रतिवादीहरूलाई अभियोग दाबीबाट "
                "सफाइ दिएको थियो।"
                ),
            "key_allegations": [
                (
                    "प्रतिवादीहरूले आपसी मिलेमतोमा गोदावरी नगरपालिकाको आर्थिक वर्ष २०७९/०८० "
                    "को निकासी शुल्क दर बागमती प्रदेश सरकारले निर्धारण गरेको प्रति घनफिट "
                    "रु.९।- भन्दा घटी प्रति घनफिट रु.२.७५।- मात्र तोकी राजस्व चुहावट गरेको "
                    "आरोप लगाइएको,"
                    ),
                (
                    "प्रतिवादीहरूले घटी दरको माध्यमबाट ठेकेदार तथा खानी उद्योग व्यवसायीहरूलाई "
                    "अनुचित लाभ पुर्‍याई नेपाल सरकार, बागमती प्रदेश सरकार र गोदावरी "
                    "नगरपालिकालाई आर्थिक हानि पुर्‍याएको,"
                    ),
                (
                    "नगरपालिकाका केही पदाधिकारीहरूले आर्थिक वर्ष २०७५/०७६ र २०७६/०७७ को "
                    "राजस्व परामर्श समितिको बैठक निर्णयसम्बन्धी सरकारी कागजात अख्तियार समक्ष "
                    "उपलब्ध नगराई लुकाएको वा नष्ट गरेको,"
                    ),
                (
                    "प्रतिवादीहरू विरुद्ध भ्रष्टाचार निवारण ऐन, २०५९ को दफा ७(ख) बमोजिम "
                    "राजस्व चुहावटसम्बन्धी कसुरमा, दफा २२ बमोजिम मतियार कसुरमा र दफा १२ "
                    "बमोजिम कागजात लुकाएको/नष्ट गरेको कसुरमा अभियोग दायर गरिएको।"
                    ),
            ],
            "accused": [
                "Gajendra Maharjan",
                "Muna Adhikari",
                "Madhusudan Dotel",
                "Nirakar Dhunga Industry Pvt. Ltd.",
                "Kedarnath Timsina",
                "Machhindranath Multipurpose Pvt. Ltd.",
                "Shashiraj Shahi",
                "Sajilo Dunga Industry Pvt. Ltd.",
            ],
        },
        "article": {
            "url": (
                "https://en.himalpress.com/special-court-acquits-godavari-mayor-13-others-in-corruption-case/"
                ),
            "title": (
                "Special Court acquits Godavari mayor, 13 others in corruption case &#8211; "
                "HimalPress | English"
                ),
            "text": (
                "**KATHMANDU:** The Special Court has acquitted 14 individuals, including the "
                "Mayor and Deputy Mayor of Godawari Municipality in Lalitpur, in a corruption "
                "case. The Commission for the Investigation of Abuse of Authority (CIAA) had "
                "filed a corruption case against 14 people, including Godavari Mayor Gajendra "
                "Maharjan and Deputy Mayor Muna Adhikari, at the Special Court on June 3. A "
                "division bench of Special Court Chairperson Tek Narayan Kunwar and Members "
                "Tejnarayan Singh Rai and Ritendra Thapa, on Wednesday, decided to acquit all "
                "14 individuals. In its charge sheet, the CIAA had claimed that officials of "
                "Godavari Municipality had awarded tenders for mining stones and sand at "
                "rates lower than government rates. The anti-graft body had claimed that it "
                "caused a loss of Rs 1.04 billion to the state. “Since the municipality can "
                "only impose fees such as export tax but cannot sell the property, co"
                ),
            "published": None,
        },
        "label": "match",
        "why": (
            "verdict (acquittal): Special Court acquits the Godavari mayor and 13 others -- "
            "this case's outcome."
            ),
        "article_from": "080-CR-0185",
    },
    {
        "case": {
            "slug": "case-081-cr-0076-dinesh-yadav-illegal-assets",
            "court_case_no": "081-CR-0076",
            "title": (
                "बागमती नगर जग्गा एकिकरण आयोजनाका अमिन दिनेश प्रसाद यादव विरुद्ध रु. ३ करोड "
                "२९ लाख स्रोत नखुलेको सम्पत्ति आर्जनको मुद्दा (081-CR-0076)"
                ),
            "short_description": (
                "गौरीघाटस्थित बागमती नगर जग्गा एकिकरण आयोजनाका करार अमिन दिनेश प्रसाद यादवले "
                "वैध आयभन्दा बढी खर्च गरी रु. ३ करोड २९ लाख स्रोत नखुलेको सम्पत्ति आर्जन "
                "गरेको आरोप।"
                ),
            "key_allegations": [
                (
                    "उपत्यका विकास प्राधिकरणअन्तर्गत बागमती नगर जग्गा एकिकरण आयोजना, "
                    "गौरीघाटमा अमिन (करार) पदमा कार्यरत दिनेश प्रसाद यादवले मिति २०६३/०५/०८ "
                    "देखि २०८१/०३/३० सम्मको जाँच अवधिमा रु. २ करोड ४२ लाखभन्दा बढी वैध आयको "
                    "तुलनामा रु. ५ करोड ७१ लाखभन्दा बढी खर्च तथा लगानी गरी रु. ३ करोड २९ "
                    "लाखभन्दा बढी श्रोत नखुलेको गैरकानूनी सम्पत्ति आर्जन गरेको।"
                    ),
                (
                    "दिनेश प्रसाद यादवले सार्वजनिक पदको अवधिमा वैध आयभन्दा बढी रकम घर "
                    "निर्माण, जग्गा खरिद, गरगहना, शेयर तथा सवारी साधन खरिदलगायतमा खर्च गरी "
                    "आफ्नो आय श्रोतसँग नमिल्ने सम्पत्ति जोडेको।"
                    ),
                (
                    "दिनेश प्रसाद यादवले गैरकानूनी रूपमा आर्जन गरेको र सोबाट बढेबढाएको "
                    "सम्पत्ति आफ्नी श्रीमती अमृता कुमारी यादवको नाममा राखी लुकाउने प्रयास "
                    "गरेको।"
                    ),
            ],
            "accused": [
                "Dinesh Prasad Yadav",
                "Amrita Kumari Yadav",
            ],
        },
        "article": {
            "url": (
                "https://www.ratopati.com/story/569777/yadav-who-worked-as-an-amin-found-guilty-in-illegal-wealth-acquisition-case-fined-rs-12-million-and-sentenced-to-imprisonment"
                ),
            "title": (
                "गैरकानुनी सम्पत्ति आर्जन मुद्दामा अमिन पदमा कार्यरत यादव दोषी ठहर, १ करोड १२ "
                "लाख बिगो र कैद सजाय | Nepal's first 24-hour updated news portal - Ratopati"
                ),
            "text": (
                "## गैरकानुनी सम्पत्ति आर्जन मुद्दामा अमिन पदमा कार्यरत यादव दोषी ठहर, १ करोड "
                "१२ लाख बिगो र कैद सजाय ## सारांश - विशेष अदालतले गैरकानुनी रूपमा सम्पत्ति "
                "आर्जन गरेको अभियोगमा वाग्मती नगर जग्गा एकीकरण आयोजना गौरीघाटमा करारमा अमिन "
                "पदमा कार्यरत दिनेशप्रसाद यादवलाई दोषी ठहर गरेको छ । - अदालतले यादवलाई १ वर्ष "
                "कैद र गैरकानुनी रूपमा आर्जन गरेको देखिएको १ करोड १२ लाख ७० हजार ६ सय ६८ "
                "रुपैयाँ ६२ पैसा बराबर नै जरिवाना हुने फैसला सुनाएको छ । - यादवको वैध आयको "
                "तुलनामा १ करोड १२ लाख ७० हजार ६ सय ६८ रुपैयाँ ६२ पैसा बराबरको सम्पत्तिको "
                "स्रोत नखुलेको र सो रकम गैरकानुनी रूपमा आर्जन गरेको अदालतको ठहर छ । काठमाडौँ "
                "। विशेष अदालतले गैरकानुनी रूपमा सम्पत्ति आर्जन गरेको अभियोगमा वाग्मती नगर "
                "जग्गा एकीकरण आयोजना गौरीघाटमा करारमा अमिन पदमा कार्यरत दिनेशप्रसाद यादवलाई "
                "दोषी ठहर गरेको छ । शेष अदालत काठमाडौँका अध्यक्ष सुदर्शनदेव भट्ट तथा सदस्यहरू "
                "उमेश कोइराला र विदुर कोइरालाको इजलासले उक्त फैसला सुनाएको हो । अदालतले यादवल"
                ),
            "published": None,
        },
        "label": "match",
        "why": (
            "verdict, Nepali: Dinesh Prasad Yadav of the Bagmati Nagar land project found "
            "guilty of illegal asset accumulation."
            ),
        "article_from": "081-CR-0076",
    },
    {
        "case": {
            "slug": "security-printing-vikal-poudel-080-cr-0172",
            "court_case_no": "080-CR-0172",
            "title": (
                "सुरक्षण मुद्रण केन्द्र उपकरण खरिदमा म्याक्स इन्टरनेशनललाई फाइदा पुर्‍याई "
                "सरकारलाई ४० करोड ७५ लाख हानि: कार्यकारी निर्देशक विकल पौडेल लगायत विरुद्धको "
                "भ्रष्टाचार आरोप सम्बन्धी मुद्दा (080-CR-0172)"
                ),
            "short_description": (
                "सुरक्षण मुद्रण केन्द्रमा उच्च सुरक्षा डिजिटल प्रिन्ट प्रोडक्सन सिस्टम खरिद "
                "प्रक्रियामा सार्वजनिक खरिद कानुनको उल्लङ्घन गरी मिलेमतोमा ठेक्का व्यवस्थापन "
                "गरी सरकारी कोषमा हानि पुर्‍याएको आरोपमा अख्तियार दुरुपयोग अनुसन्धान आयोगले "
                "तत्कालीन कार्यकारी निर्देशक विकल पौडेल, अन्य प्रतिवादी कर्मचारीहरू, तथा "
                "ठेक्का प्राप्त कम्पनी म्याक्स इन्टरनेशनल र यसका सि.इ.ओ. अकबर हुसेन विरुद्ध "
                "भ्रष्टाचार निवारण ऐन, २०५९ अन्तर्गत अभियोग दायर गरेको थियो। विशेष अदालतले "
                "विकल पौडेल तथा अकबर हुसेन र म्याक्स इन्टरनेशनललाई दोषी ठहर गर्दै अन्य "
                "प्रतिवादी कर्मचारीहरूलाई सफाइ दिएको थियो।"
                ),
            "key_allegations": [
                (
                    "प्रतिवादीहरूले सुरक्षण मुद्रण केन्द्रको प्रिन्ट प्रोडक्सन सिस्टम खरिद "
                    "गर्दा कानुनले अनिवार्य गरेको खरिद इकाईको गठन, सम्भाव्यता अध्ययन र "
                    "समितिबाट स्वीकृति नलिई खरिद प्रक्रिया अघि बढाएको।"
                    ),
                (
                    "प्रतिवादीहरूले Card Based र Paper Based गरी छुट्टाछुट्टै कार्यक्रमका "
                    "लागि विनियोजित बजेटलाई कानुनी प्रक्रिया वा कार्यक्रम संशोधन नगरी "
                    "मिलेमतोमा एउटै कार्यक्रममा गाभी खर्च गरेको।"
                    ),
                (
                    "प्रतिवादीहरूले आवश्यकताभन्दा बढी प्रिन्टिङ प्रेस, अत्यधिक क्षमताको डाइ "
                    "कटिङ मेसिन तथा प्रयोजनविहीन Critical IT System खरिद गरी सरकारी कोषलाई "
                    "आर्थिक हानि पुर्‍याएको।"
                    ),
                (
                    "प्रतिवादीहरूले एउटा विशेष कम्पनीलाई मात्र लक्षित गरी प्राविधिक विवरण तथा "
                    "योग्यताका सर्तहरू तयार गरी प्रतिस्पर्धा सीमित पारेको र सोही मिलेमतोमा "
                    "ठेक्का प्रदान गरेको।"
                    ),
                (
                    "प्रतिवादीहरूले उपकरण आपूर्ति गर्ने विदेशी कम्पनीबाट कमिसन रकम विदेशस्थित "
                    "व्यक्तिगत बैंक खातामा प्राप्त गरी गैरकानुनी लाभ लिएको।"
                    ),
            ],
            "accused": [
                "Bikal Poudel",
                "Akbar Husen",
                "Max International",
                "Saphal Shrestha",
                "Navin Kumar Pokharel",
                "Harivallabh Ghimire",
                "Khadak Bahadur Thapa",
            ],
        },
        "article": {
            "url": (
                "https://ekantipur.com/news/2025/05/04/vikal-poudel-was-found-corrupt-in-the-purchase-of-security-printing-equipment-four-people-were-acquitted-50-18.html"
                ),
            "title": (
                "सुरक्षण मुद्रण उपकरण खरिदमा विकल पौडेल भ्रष्टाचारी ठहर, चार जनालाई सफाइ - "
                "कान्तिपुर"
                ),
            "text": (
                "काठमाडौँ — सुरक्षण मुद्रण केन्द्रका तत्कालीन कार्यकारी निर्देशक विकल पौडेल "
                "उपकरण खरिद गर्दा भ्रष्टाचार गरेको कसुरमा दोषी ठहर भएका छन् । विशेष अदालतका "
                "अध्यक्ष टेकनारायण कुँवर, सदस्यहरू तेजनारायण सिंह राई र रामबहादुर थापाको "
                "इजलासले पौडेललाई आइतबार दोषी ठहर गर्दै सजाय निर्धारण गरेको हो । पौडेललाई "
                "अदालतले १ वर्ष ६ महिना कैद र दामासाहीले १३ करोड ५८ लाख ५२ हजार ५८० रुपैयाँ "
                "बिगो बराबरको जरिवाना गर्न फैसला गरेको छ । पौडेल केन्द्रको कार्यकारी "
                "निर्देशकको भूमिकामा रहेको देखिँदा थप ६ महिना पनि कैद सजाय सुनाइएको छ । "
                "त्यस्तै मुद्दाका अन्य प्रतिवादीहरु म्याक्स इन्टरनेसनल प्रालि र सीईओ अकबर "
                "हुसेनको हकमा पनि १३ करोड ५८ लाख ५२ हजार ५८० रुपैयाँ जरिवाना र सोही बराबरको "
                "बिगो असुल गर्न फैसला भएको छ । अकबर हुसेनलाई पनि १ वर्ष ६ महिना कैद गर्ने ठहर "
                "भएको छ । विकल, म्याक्स इन्टरनेशनल प्रालि, अकबरले जनही ५४ लाख ३४ हजार १०३ "
                "रुपैयाँ क्षतिपूर्ति शुल्कबापत पीडित राहत कोषमा दाखिला गर्नुपर्ने ठहर पनि "
                "विशेषले गरेको छ ।"
                ),
            "published": None,
        },
        "label": "match",
        "why": (
            "verdict, Nepali: equipment-purchase conviction naming Max International and "
            "Akbar Husen, both accused on 080-CR-0172."
            ),
        "article_from": "080-CR-0172",
    },
    {
        "case": {
            "slug": "vikal-paudel-080-cr-0174-illegal-assets",
            "court_case_no": "080-CR-0174",
            "title": (
                "सुरक्षण मुद्रण केन्द्रका कार्यकारी निर्देशक विकल पौडेल विरुद्ध रु. ६२ करोड "
                "स्रोत नखुलेको सम्पत्ति आर्जन गरेको सम्बन्धी मुद्दा (080-CR-0174)"
                ),
            "short_description": (
                "सुरक्षण मुद्रण केन्द्रका तत्कालीन कार्यकारी निर्देशक विकल पौडेलले पदमा बहाल "
                "रहँदा स्वदेशी तथा विदेशी आपूर्तिकर्ता कम्पनीहरूबाट कमिशन रकम प्राप्त गरी "
                "सिंगापुर, अमेरिकास्थित बैंक खाता, शेयर तथा घरजग्गामार्फत रु.६२,१९,१८,६८४.२९ "
                "बराबरको स्रोत नखुलेको सम्पत्ति आर्जन गरेको र सम्पत्ति विवरणमा झुट्टा विवरण "
                "पेस गरेको आरोपमा अख्तियार दुरुपयोग अनुसन्धान आयोगले निज विरुद्ध भ्रष्टाचार "
                "निवारण ऐन, २०५९ को दफा २० र दफा १६ बमोजिम अभियोग दायर गरेको थियो। निजकी "
                "श्रीमती एलिना बस्नेत, बाबु धर्म प्रसाद शर्मा पौडेल र आमा शितल पौडेललाई "
                "उनीहरूको नाममा रहेको सम्पत्ति जफत गर्ने प्रयोजनका लागि मात्र प्रतिवादी कायम "
                "गरिएको थियो। यो मुद्दा हाल विशेष अदालतमा विचाराधीन छ।"
                ),
            "key_allegations": [
                (
                    "प्रतिवादी विकल पौडेलले सुरक्षण मुद्रण केन्द्रको कार्यकारी निर्देशक पदमा "
                    "रहँदा स्वदेशी तथा विदेशी आपूर्तिकर्ता कम्पनीहरूबाट खरिद ठेक्कावापत कमिशन "
                    "रकम प्राप्त गरी विदेशी बैंक खातामा जम्मा गरेको,"
                    ),
                (
                    "प्रतिवादीले राष्ट्रसेवक भएको तथ्य लुकाई विदेशी कम्पनीहरूमा परामर्शदाता "
                    "तथा बिक्री एजेन्टको गोप्य रूपमा काम गरी थप कमिशन रकम प्राप्त गरेको,"
                    ),
                (
                    "प्रतिवादीले गैरकानूनी रूपमा प्राप्त रकमलाई विदेशी बैंक खाता, बिमा, शेयर "
                    "तथा घरजग्गामा लगानी गरी आफू, श्रीमती र परिवारका अन्य सदस्यहरूको नाममा "
                    "सम्पत्ति आर्जन गरी लुकाई राखेको,"
                    ),
                (
                    "प्रतिवादीले आफूले पेस गर्नुपर्ने सम्पत्ति विवरणमा वास्तविक सम्पत्ति "
                    "नखुलाई झुट्टा विवरण पेस गरेको,"
                    ),
                (
                    "प्रतिवादी विकल पौडेल विरुद्ध भ्रष्टाचार निवारण ऐन, २०५९ को दफा २० (स्रोत "
                    "नखुलेको सम्पत्ति आर्जन) र दफा १६ (झुट्टा सम्पत्ति विवरण) बमोजिम "
                    "भ्रष्टाचारसम्बन्धी कसुरमा, तथा परिवारका सदस्यहरू विरुद्ध सम्बद्ध "
                    "सम्पत्ति जफत/असुल उपरको प्रयोजनका लागि अभियोग दायर गरिएको,"
                    ),
            ],
            "accused": [
                "Bikal Poudel",
                "Elina Basnet",
                "Shital Poudel",
                "Dharm Prasad Poudel (Dharm Prasad Sharma Poudel)",
            ],
        },
        "article": {
            "url": (
                "https://english.ratopati.com/story/60193/hearing-underway-in-corruption-case-against-bikal-poudel"
                ),
            "title": (
                "Special Court to Hear Corruption Case Against Former Security Printing "
                "Center Chief Bikal Paudel | Ratopati | No.1 Nepali News Portal"
                ),
            "text": (
                "# Special Court to Hear Corruption Case Against Former Security Printing "
                "Center Chief Bikal Paudel Kathmandu. A hearing has been scheduled at the "
                "Special Court regarding the corruption case involving illegal wealth "
                "accumulation against Bikal Paudel, the former Executive Director of the "
                "Security Printing Center. The case is set to be heard by a bench comprising "
                "Special Court Chairman Sudarshan Dev Bhatta and members Hemant Rawal and "
                "Umesh Koirala. On २०८१ जेठ ९, the Commission for the Investigation of Abuse "
                "of Authority (CIAA) filed a corruption case against Paudel, accusing him of "
                "amassing illegal assets worth 621,918,684.29 rupees. The commission alleged "
                "that Paudel hid funds in Singapore and the United States under the name of "
                "his wife, Alina Basnet. The CIAA claims that Paudel accepted commissions "
                "from suppliers and funneled the money through various individuals. The "
                "commission assert"
                ),
            "published": None,
        },
        "label": "match",
        "why": (
            "hearing: explicitly the ILLEGAL WEALTH case, Rs 621,918,684.29 -- the only one "
            "of this case's three bound articles that is about it."
            ),
        "article_from": "080-CR-0174",
    },
    {
        "case": {
            "slug": "case-080-cr-0151-krishna-subedi-embezzlement",
            "court_case_no": "080-CR-0151",
            "title": (
                "श्री सदाशिव प्राथमिक विद्यालय, कास्कीकोट: प्रधानाध्यापक कृष्णप्रसाद सुवेदी "
                "लगायतले आम्दानी–खर्च लुकाई रु. ३.७७ लाख हिनामिना गरेको भ्रष्टाचार मुद्दा "
                "(080-CR-0151)"
                ),
            "short_description": (
                "कास्कीकोटस्थित श्री सदाशिव प्राथमिक विद्यालयका तत्कालीन प्रधानाध्यापक "
                "कृष्णप्रसाद सुवेदीले आम्दानी–खर्च विवरण र लेखापरीक्षण प्रतिवेदनमा गलत विवरण "
                "राखी विद्यालयको रु. ३.७७ लाखभन्दा बढी हिनामिना गरेको आरोप।"
                ),
            "key_allegations": [
                (
                    "श्री सदाशिव प्राथमिक विद्यालय, कास्कीकोटका तत्कालीन प्रधानाध्यापक कृष्ण "
                    "प्रसाद सुवेदीले विद्यालयको आ.व. २०६८/६९ र २०६९/७० को आम्दानी–खर्च "
                    "विवरणमा जिल्ला शिक्षा कार्यालयबाट निकासा भएको रकम आयमा नदेखाई, नभएको "
                    "आम्दानी र खर्च लेखापरीक्षण प्रतिवेदनमा प्रविष्ट गराई तथा भवन निर्माण "
                    "समितिको निर्णयमा आफूखुसी थपघट गरी विद्यालयको रु. ३ लाख ७७ हजारभन्दा बढी "
                    "रकम हानिनोक्सानी पुर्‍याएको।"
                    ),
                (
                    "तत्कालीन प्रधानाध्यापक सुवेदीले खर्च पुष्ट्याउने बिल भरपाई बिना नभएको "
                    "कामलाई खर्च देखाई विद्युतीय माध्यमबाट तयार लेखापरीक्षण प्रतिवेदनमा गलत "
                    "विवरण समावेश गरेको।"
                    ),
                (
                    "तत्कालीन लेखापरीक्षक कृष्ण प्रसाद गौतमले विद्यालयको वास्तविक "
                    "आम्दानी–खर्चसँग नमिल्ने लेखापरीक्षण प्रतिवेदन तयार गरी गलत विवरण "
                    "प्रमाणित गर्न सहयोग गरेको।"
                    ),
            ],
            "accused": [
                "Krishna Prasad Subedi",
                "Krishna Prasad Gautam",
            ],
        },
        "article": {
            "url": "https://baahrakhari.com/detail/417574",
            "title": (
                "सदाशिव विद्यालयका तत्कालीन प्रधानाध्यापकसहित दुईजनाविरुद्ध भ्रष्टाचार मुद्दा "
                "- बाह्रखरी :: Baahrakhari"
                ),
            "text": (
                "काठमाडौं । अख्तियार दुरुपयोग अनुसन्धान आयोगले कास्कीको सदाशिव प्राथमिक "
                "विद्यालयका तत्कालीन प्रधानाध्यापक कृष्णप्रसाद सुवेदीसहित दुईजनाविरुद्ध "
                "भ्रष्टाचार मुद्दा दायर गरेको छ । उनीहरूले तीन लाख ७७ हजार २३६ रुपैयाँ "
                "बराबरको सार्वजनिक सम्पत्ति हानिनोक्सानी गरी भ्रष्टाचार गरेको ठहर गर्दै "
                "अख्तियारले बुधबार विशेष अदालत काठमाडौंमा मुद्दा दायर गरेको हो । विद्यालयका "
                "प्रधानाध्यापकले हिसाब किताब आम्दानी खर्च विवरणको हरहिसाब नबुझाएको भन्ने "
                "उजुरी परेपछि अख्तियारले अनुसन्धान सुरु गरेको थियो । अनुसन्धान हुँदा २०६८ "
                "साउनदेखि २०६९ असार र २०६९ साउनदेखि २०७० असार महिनासम्मको अवधिमा विद्यालयको "
                "रकम लिने÷खाने नियत राखी विद्यालयमा भएको भवन निर्माण समितिको निर्णयमा आफूखुसी "
                "थपघट समेत गरी सरकारलाई तीन लाख ७७ हजार २३६ रुपैयाँ हानिनोक्सानी गरेको पाइएको "
                "अख्तियारले जनाएको छ । भ्रष्टाचार गरेको पाइएपछि अख्यिारले तत्कालीन "
                "प्रधानाध्यापक कृष्णप्रसाद सुवेदी र सोही विद्यालयका तत्कालीन लेखापरीक्षक "
                "कृष्णप्रसाद गौतमविरुद्ध मुद्दा"
                ),
            "published": None,
        },
        "label": "match",
        "why": (
            "filing, Nepali, small amount: Rs 3,77,236 at Sadashiv Primary School, Kaski -- "
            "matches the case exactly."
            ),
        "article_from": "080-CR-0151",
    },
    {
        "case": {
            "slug": "vikal-paudel-080-cr-0174-illegal-assets",
            "court_case_no": "080-CR-0174",
            "title": (
                "सुरक्षण मुद्रण केन्द्रका कार्यकारी निर्देशक विकल पौडेल विरुद्ध रु. ६२ करोड "
                "स्रोत नखुलेको सम्पत्ति आर्जन गरेको सम्बन्धी मुद्दा (080-CR-0174)"
                ),
            "short_description": (
                "सुरक्षण मुद्रण केन्द्रका तत्कालीन कार्यकारी निर्देशक विकल पौडेलले पदमा बहाल "
                "रहँदा स्वदेशी तथा विदेशी आपूर्तिकर्ता कम्पनीहरूबाट कमिशन रकम प्राप्त गरी "
                "सिंगापुर, अमेरिकास्थित बैंक खाता, शेयर तथा घरजग्गामार्फत रु.६२,१९,१८,६८४.२९ "
                "बराबरको स्रोत नखुलेको सम्पत्ति आर्जन गरेको र सम्पत्ति विवरणमा झुट्टा विवरण "
                "पेस गरेको आरोपमा अख्तियार दुरुपयोग अनुसन्धान आयोगले निज विरुद्ध भ्रष्टाचार "
                "निवारण ऐन, २०५९ को दफा २० र दफा १६ बमोजिम अभियोग दायर गरेको थियो। निजकी "
                "श्रीमती एलिना बस्नेत, बाबु धर्म प्रसाद शर्मा पौडेल र आमा शितल पौडेललाई "
                "उनीहरूको नाममा रहेको सम्पत्ति जफत गर्ने प्रयोजनका लागि मात्र प्रतिवादी कायम "
                "गरिएको थियो। यो मुद्दा हाल विशेष अदालतमा विचाराधीन छ।"
                ),
            "key_allegations": [
                (
                    "प्रतिवादी विकल पौडेलले सुरक्षण मुद्रण केन्द्रको कार्यकारी निर्देशक पदमा "
                    "रहँदा स्वदेशी तथा विदेशी आपूर्तिकर्ता कम्पनीहरूबाट खरिद ठेक्कावापत कमिशन "
                    "रकम प्राप्त गरी विदेशी बैंक खातामा जम्मा गरेको,"
                    ),
                (
                    "प्रतिवादीले राष्ट्रसेवक भएको तथ्य लुकाई विदेशी कम्पनीहरूमा परामर्शदाता "
                    "तथा बिक्री एजेन्टको गोप्य रूपमा काम गरी थप कमिशन रकम प्राप्त गरेको,"
                    ),
                (
                    "प्रतिवादीले गैरकानूनी रूपमा प्राप्त रकमलाई विदेशी बैंक खाता, बिमा, शेयर "
                    "तथा घरजग्गामा लगानी गरी आफू, श्रीमती र परिवारका अन्य सदस्यहरूको नाममा "
                    "सम्पत्ति आर्जन गरी लुकाई राखेको,"
                    ),
                (
                    "प्रतिवादीले आफूले पेस गर्नुपर्ने सम्पत्ति विवरणमा वास्तविक सम्पत्ति "
                    "नखुलाई झुट्टा विवरण पेस गरेको,"
                    ),
                (
                    "प्रतिवादी विकल पौडेल विरुद्ध भ्रष्टाचार निवारण ऐन, २०५९ को दफा २० (स्रोत "
                    "नखुलेको सम्पत्ति आर्जन) र दफा १६ (झुट्टा सम्पत्ति विवरण) बमोजिम "
                    "भ्रष्टाचारसम्बन्धी कसुरमा, तथा परिवारका सदस्यहरू विरुद्ध सम्बद्ध "
                    "सम्पत्ति जफत/असुल उपरको प्रयोजनका लागि अभियोग दायर गरिएको,"
                    ),
            ],
            "accused": [
                "Bikal Poudel",
                "Elina Basnet",
                "Shital Poudel",
                "Dharm Prasad Poudel (Dharm Prasad Sharma Poudel)",
            ],
        },
        "article": {
            "url": (
                "https://ekantipur.com/en/news/2024/05/12/purchase-of-security-printing-equipment-corruption-case-filed-against-6-people-including-vikal-paudel-54-40.html"
                ),
            "title": (
                "Purchase of security printing equipment: Corruption case filed against 6 "
                "people including Vikal Paudel - कान्तिपुर"
                ),
            "text": (
                "A corruption case has been filed against Vikal Poudel, the then executive "
                "director of Surakshan Printing Center, Safal Shrestha, the then director of "
                "National Information Technology Center, and the company on the charge of "
                "irregularities in the purchase of security printing equipment. The "
                "Commission for Investigation of Abuse of Authority has filed a corruption "
                "case in a special court demanding more than 40 million begos on Sunday. "
                "Surakshan Printing Center, Kathmandu has mentioned that while calling for "
                "tenders for the purchase of software and other equipment of Surakshan Press, "
                "it should be in accordance with the prevailing laws related to public "
                "procurement, and it has been found that the property of the Government of "
                "Nepal has been unlawfully harmed as there is no healthy competition with "
                "certain limited individuals or companies. . The authority has mentioned in "
                "the charge sheet that"
                ),
            "published": None,
        },
        "label": "no_match",
        "why": (
            "HARD, same accused different case, AND A LIVE PROD MIS-BIND: the case is Rs 62cr "
            "illegal assets (080-CR-0174); the article is the Rs 40.75cr equipment-purchase "
            "case (080-CR-0172). Bound in prod anyway."
            ),
        "article_from": "080-CR-0172 (the equipment-purchase case; bound to 0174 in prod)",
    },
    {
        "case": {
            "slug": "vikal-paudel-080-cr-0174-illegal-assets",
            "court_case_no": "080-CR-0174",
            "title": (
                "सुरक्षण मुद्रण केन्द्रका कार्यकारी निर्देशक विकल पौडेल विरुद्ध रु. ६२ करोड "
                "स्रोत नखुलेको सम्पत्ति आर्जन गरेको सम्बन्धी मुद्दा (080-CR-0174)"
                ),
            "short_description": (
                "सुरक्षण मुद्रण केन्द्रका तत्कालीन कार्यकारी निर्देशक विकल पौडेलले पदमा बहाल "
                "रहँदा स्वदेशी तथा विदेशी आपूर्तिकर्ता कम्पनीहरूबाट कमिशन रकम प्राप्त गरी "
                "सिंगापुर, अमेरिकास्थित बैंक खाता, शेयर तथा घरजग्गामार्फत रु.६२,१९,१८,६८४.२९ "
                "बराबरको स्रोत नखुलेको सम्पत्ति आर्जन गरेको र सम्पत्ति विवरणमा झुट्टा विवरण "
                "पेस गरेको आरोपमा अख्तियार दुरुपयोग अनुसन्धान आयोगले निज विरुद्ध भ्रष्टाचार "
                "निवारण ऐन, २०५९ को दफा २० र दफा १६ बमोजिम अभियोग दायर गरेको थियो। निजकी "
                "श्रीमती एलिना बस्नेत, बाबु धर्म प्रसाद शर्मा पौडेल र आमा शितल पौडेललाई "
                "उनीहरूको नाममा रहेको सम्पत्ति जफत गर्ने प्रयोजनका लागि मात्र प्रतिवादी कायम "
                "गरिएको थियो। यो मुद्दा हाल विशेष अदालतमा विचाराधीन छ।"
                ),
            "key_allegations": [
                (
                    "प्रतिवादी विकल पौडेलले सुरक्षण मुद्रण केन्द्रको कार्यकारी निर्देशक पदमा "
                    "रहँदा स्वदेशी तथा विदेशी आपूर्तिकर्ता कम्पनीहरूबाट खरिद ठेक्कावापत कमिशन "
                    "रकम प्राप्त गरी विदेशी बैंक खातामा जम्मा गरेको,"
                    ),
                (
                    "प्रतिवादीले राष्ट्रसेवक भएको तथ्य लुकाई विदेशी कम्पनीहरूमा परामर्शदाता "
                    "तथा बिक्री एजेन्टको गोप्य रूपमा काम गरी थप कमिशन रकम प्राप्त गरेको,"
                    ),
                (
                    "प्रतिवादीले गैरकानूनी रूपमा प्राप्त रकमलाई विदेशी बैंक खाता, बिमा, शेयर "
                    "तथा घरजग्गामा लगानी गरी आफू, श्रीमती र परिवारका अन्य सदस्यहरूको नाममा "
                    "सम्पत्ति आर्जन गरी लुकाई राखेको,"
                    ),
                (
                    "प्रतिवादीले आफूले पेस गर्नुपर्ने सम्पत्ति विवरणमा वास्तविक सम्पत्ति "
                    "नखुलाई झुट्टा विवरण पेस गरेको,"
                    ),
                (
                    "प्रतिवादी विकल पौडेल विरुद्ध भ्रष्टाचार निवारण ऐन, २०५९ को दफा २० (स्रोत "
                    "नखुलेको सम्पत्ति आर्जन) र दफा १६ (झुट्टा सम्पत्ति विवरण) बमोजिम "
                    "भ्रष्टाचारसम्बन्धी कसुरमा, तथा परिवारका सदस्यहरू विरुद्ध सम्बद्ध "
                    "सम्पत्ति जफत/असुल उपरको प्रयोजनका लागि अभियोग दायर गरिएको,"
                    ),
            ],
            "accused": [
                "Bikal Poudel",
                "Elina Basnet",
                "Shital Poudel",
                "Dharm Prasad Poudel (Dharm Prasad Sharma Poudel)",
            ],
        },
        "article": {
            "url": (
                "https://kathmandupost.com/national/2024/11/28/special-court-sentences-paudel-and-shrestha-to-eight-years-for-corruption-in-security-printing-case"
                ),
            "title": (
                "Court sentences Paudel and Shrestha to eight years for corruption in "
                "security printing case"
                ),
            "text": (
                "# Court sentences Paudel and Shrestha to eight years for corruption in "
                "security printing case While Paudel and Shrestha were found guilty, former "
                "chief secretary Baikuntha Aryal and several others named in the case were "
                "acquitted by the court.The Special Court has sentenced Bikal Paudel, former "
                "executive director of the Security Printing Centre, and Saphal Shrestha, "
                "former director of the centre, to eight years in prison each for their "
                "involvement in a corruption case related to the printing of excise duty "
                "stickers. The court also ordered both to pay a restitution amount of Rs34.22 "
                "million and an equivalent fine. The full court comprising chair Tek Narayan "
                "Kunwar and justices Khushi Prasad Tharu and Ritendra Thapa, delivered the "
                "verdict following an investigation into irregularities in the procurement "
                "and printing process. The court on October 30 convicted Paudel and Shrestha "
                "and determi"
                ),
            "published": None,
        },
        "label": "no_match",
        "why": (
            "HARD, same accused THIRD case, AND A LIVE PROD MIS-BIND: excise duty sticker "
            "printing, Rs 34.22m restitution -- neither this case's Rs 62cr nor 0172's Rs "
            "40.75cr. The prod note itself says 'अर्को भ्रष्टाचार मुद्दा'."
            ),
        "article_from": "a third Bikal Poudel case, excise-duty sticker printing (bound to 0174 in prod)",
    },
    {
        "case": {
            "slug": "security-printing-vikal-poudel-080-cr-0172",
            "court_case_no": "080-CR-0172",
            "title": (
                "सुरक्षण मुद्रण केन्द्र उपकरण खरिदमा म्याक्स इन्टरनेशनललाई फाइदा पुर्‍याई "
                "सरकारलाई ४० करोड ७५ लाख हानि: कार्यकारी निर्देशक विकल पौडेल लगायत विरुद्धको "
                "भ्रष्टाचार आरोप सम्बन्धी मुद्दा (080-CR-0172)"
                ),
            "short_description": (
                "सुरक्षण मुद्रण केन्द्रमा उच्च सुरक्षा डिजिटल प्रिन्ट प्रोडक्सन सिस्टम खरिद "
                "प्रक्रियामा सार्वजनिक खरिद कानुनको उल्लङ्घन गरी मिलेमतोमा ठेक्का व्यवस्थापन "
                "गरी सरकारी कोषमा हानि पुर्‍याएको आरोपमा अख्तियार दुरुपयोग अनुसन्धान आयोगले "
                "तत्कालीन कार्यकारी निर्देशक विकल पौडेल, अन्य प्रतिवादी कर्मचारीहरू, तथा "
                "ठेक्का प्राप्त कम्पनी म्याक्स इन्टरनेशनल र यसका सि.इ.ओ. अकबर हुसेन विरुद्ध "
                "भ्रष्टाचार निवारण ऐन, २०५९ अन्तर्गत अभियोग दायर गरेको थियो। विशेष अदालतले "
                "विकल पौडेल तथा अकबर हुसेन र म्याक्स इन्टरनेशनललाई दोषी ठहर गर्दै अन्य "
                "प्रतिवादी कर्मचारीहरूलाई सफाइ दिएको थियो।"
                ),
            "key_allegations": [
                (
                    "प्रतिवादीहरूले सुरक्षण मुद्रण केन्द्रको प्रिन्ट प्रोडक्सन सिस्टम खरिद "
                    "गर्दा कानुनले अनिवार्य गरेको खरिद इकाईको गठन, सम्भाव्यता अध्ययन र "
                    "समितिबाट स्वीकृति नलिई खरिद प्रक्रिया अघि बढाएको।"
                    ),
                (
                    "प्रतिवादीहरूले Card Based र Paper Based गरी छुट्टाछुट्टै कार्यक्रमका "
                    "लागि विनियोजित बजेटलाई कानुनी प्रक्रिया वा कार्यक्रम संशोधन नगरी "
                    "मिलेमतोमा एउटै कार्यक्रममा गाभी खर्च गरेको।"
                    ),
                (
                    "प्रतिवादीहरूले आवश्यकताभन्दा बढी प्रिन्टिङ प्रेस, अत्यधिक क्षमताको डाइ "
                    "कटिङ मेसिन तथा प्रयोजनविहीन Critical IT System खरिद गरी सरकारी कोषलाई "
                    "आर्थिक हानि पुर्‍याएको।"
                    ),
                (
                    "प्रतिवादीहरूले एउटा विशेष कम्पनीलाई मात्र लक्षित गरी प्राविधिक विवरण तथा "
                    "योग्यताका सर्तहरू तयार गरी प्रतिस्पर्धा सीमित पारेको र सोही मिलेमतोमा "
                    "ठेक्का प्रदान गरेको।"
                    ),
                (
                    "प्रतिवादीहरूले उपकरण आपूर्ति गर्ने विदेशी कम्पनीबाट कमिसन रकम विदेशस्थित "
                    "व्यक्तिगत बैंक खातामा प्राप्त गरी गैरकानुनी लाभ लिएको।"
                    ),
            ],
            "accused": [
                "Bikal Poudel",
                "Akbar Husen",
                "Max International",
                "Saphal Shrestha",
                "Navin Kumar Pokharel",
                "Harivallabh Ghimire",
                "Khadak Bahadur Thapa",
            ],
        },
        "article": {
            "url": (
                "https://english.ratopati.com/story/60193/hearing-underway-in-corruption-case-against-bikal-poudel"
                ),
            "title": (
                "Special Court to Hear Corruption Case Against Former Security Printing "
                "Center Chief Bikal Paudel | Ratopati | No.1 Nepali News Portal"
                ),
            "text": (
                "# Special Court to Hear Corruption Case Against Former Security Printing "
                "Center Chief Bikal Paudel Kathmandu. A hearing has been scheduled at the "
                "Special Court regarding the corruption case involving illegal wealth "
                "accumulation against Bikal Paudel, the former Executive Director of the "
                "Security Printing Center. The case is set to be heard by a bench comprising "
                "Special Court Chairman Sudarshan Dev Bhatta and members Hemant Rawal and "
                "Umesh Koirala. On २०८१ जेठ ९, the Commission for the Investigation of Abuse "
                "of Authority (CIAA) filed a corruption case against Paudel, accusing him of "
                "amassing illegal assets worth 621,918,684.29 rupees. The commission alleged "
                "that Paudel hid funds in Singapore and the United States under the name of "
                "his wife, Alina Basnet. The CIAA claims that Paudel accepted commissions "
                "from suppliers and funneled the money through various individuals. The "
                "commission assert"
                ),
            "published": None,
        },
        "label": "no_match",
        "why": (
            "HARD, the mirror direction: procurement case 080-CR-0172 offered the "
            "illegal-assets article. Same person, same institution, same court."
            ),
        "article_from": "080-CR-0174",
    },
    {
        "case": {
            "slug": "case-080-cr-0032-toran-karki-illegal-assets",
            "court_case_no": "080-CR-0032",
            "title": (
                "जलस्रोत अनुसन्धान विकास केन्द्रका लेखा अधिकृत तोरण बहादुर कार्कीले रु.४.६७ "
                "करोड स्रोत नखुलेको सम्पत्ति आर्जन गरेको आरोप (080-CR-0032)"
                ),
            "short_description": (
                "जलस्रोत अनुसन्धान विकास केन्द्र, पुल्चोकका लेखा अधिकृत तोरण बहादुर कार्कीले "
                "वैध आयभन्दा बढी खर्च-लगानी गरी करिब रु.४.६७ करोड स्रोत नखुलेको सम्पत्ति "
                "आर्जन गरी श्रीमतीसमेतको नाममा लुकाएको आरोप।"
                ),
            "key_allegations": [
                (
                    "जलस्रोत अनुसन्धान विकास केन्द्र, पुल्चोकका लेखा अधिकृत तोरण बहादुर "
                    "कार्कीले सार्वजनिक सेवा प्रवेश गरेको मिति २०५३।०२।१७ देखि २०७९।०८।२८ "
                    "सम्मको जाँच अवधिमा वैध आय रु. १ करोड ८ लाखभन्दा बढी हुँदा पनि घर "
                    "निर्माण, जग्गा खरिद, शेयर लगानी, सवारी साधन खरिद र बैंक मौज्दात लगायतमा "
                    "रु. ५ करोड ७५ लाखभन्दा बढी खर्च तथा लगानी गरी करिब रु. ४ करोड ६७ लाख "
                    "स्रोत नखुलेको सम्पत्ति आर्जन गरेको।"
                    ),
                (
                    "तोरण बहादुर कार्कीले वैध आयसँग नमिल्ने खर्च तथा लगानीबाट आफू र परिवारको "
                    "अमिल्दो तथा अस्वाभाविक उच्चस्तरको जीवनयापन कायम गरेको।"
                    ),
                (
                    "तोरण बहादुर कार्कीले स्रोत नखुलेको सम्पत्ति श्रीमती उर्मिला थापा "
                    "कार्कीसमेतको नाममा जग्गा, शेयर, सवारी साधन र बैंक मौज्दातमा राखी "
                    "सम्पत्ति लुकाउने वा स्थानान्तरण गर्ने काम गरेको।"
                    ),
            ],
            "accused": [
                "Toran Bahadur Karki",
                "Urmila Thapa Karki",
            ],
        },
        "article": {
            "url": (
                "https://myrepublica.nagariknetwork.com/news/ciaa-files-case-against-section-officer-who-registered-govt-land-in-name-of-individual/"
                ),
            "title": (
                "CIAA files case against section officer who registered govt land in name of "
                "individual - myRepublica - The New York Times Partner, Latest news of Nepal "
                "in English, Latest News Articles | Republica"
                ),
            "text": (
                "KATHMANDU, March 21: The Commission for the Investigation of Abuse of "
                "Authority (CIAA) has filed a case against a section officer who registered "
                "government land in the name of an individual. The CIAA filed the case at the "
                "Special Court on Thursday against Yam Kumar Karki, a section officer working "
                "in Bagmati Nagar Land Integration Project Office under the Kathmandu Valley "
                "Development Authority. ### Land mafia register 4.5 bighas of govt land in "
                "individual’s nam... According to the CIAA, Karki engaged in corruption "
                "activities by illegally registering the government land in the name of an "
                "individual. Karki joined government service in mid-July 2001. As of April "
                "13, 2023, he has accumulated assets amounting to Rs 37.7 million and has "
                "invested Rs 69.8 million in various sectors, according to the CIAA findings. "
                "The source of Karki's assets, totaling Rs 32.1 million, remains undisclosed. "
                "The C"
                ),
            "published": None,
        },
        "label": "no_match",
        "why": (
            "HARD, shared surname + same scheme + same fiscal year: Toran Bahadur KARKI "
            "(080-CR-0032, Rs 4.67cr) offered Yam Kumar KARKI's article (080-CR-0136, Rs "
            "3.21cr). Both illegal assets, both FY080, both Special Court."
            ),
        "article_from": "080-CR-0136",
    },
    {
        "case": {
            "slug": "yam-kumar-karki-080-cr-0136-illegal-assets",
            "court_case_no": "080-CR-0136",
            "title": (
                "काठमाडौं उपत्यका विकास प्राधिकरणका शाखा अधिकृत याम कुमार कार्कीले रु.३ करोड "
                "२१ लाख स्रोत नखुलेको सम्पत्ति आर्जन गरेको आरोप सम्बन्धी मुद्दा (080-CR-0136)"
                ),
            "short_description": (
                "काठमाडौं उपत्यका विकास प्राधिकरणमा शाखा अधिकृत (करार) याम कुमार कार्कीले "
                "सार्वजनिक पदमा बहाल रहँदा वैध आयभन्दा बढी सम्पत्ति आर्जन गरी श्रीमती देवका "
                "अधिकारी र छोरी गृष्मा कार्कीको नाममा समेत राखेको आरोपमा अख्तियार दुरुपयोग "
                "अनुसन्धान आयोगले गैरकानुनी सम्पत्ति आर्जनसम्बन्धी भ्रष्टाचार अभियोग दायर "
                "गरेको थियो। आयोगले आयभन्दा व्यय रु.३,२१,३२,९५६।७५ ले बढी देखाई बिगो कायम "
                "गरेको थियो। विशेष अदालतले प्रमाणहरूको पुनर्मूल्याङ्कन गर्दा वैध आय नै "
                "व्ययभन्दा बढी रहेको ठहर गर्दै प्रतिवादीलाई सफाइ दिएको थियो।"
                ),
            "key_allegations": [
                (
                    "याम कुमार कार्कीले काठमाडौं उपत्यका विकास प्राधिकरणसहितका निकायमा "
                    "सार्वजनिक पद धारण गरी पदको दुरुपयोग गर्दै जाँच अवधिभर वैध आयभन्दा "
                    "रु.३,२१,३२,९५६।७५ बढी मूल्यको स्रोत नखुलेको सम्पत्ति आर्जन गरी सोको केही "
                    "अंश श्रीमती देवका अधिकारी र छोरी गृष्मा कार्कीको नाममा राखी वास्तविक "
                    "स्वामित्व लुकाएको।"
                    ),
            ],
            "accused": [
                "Yam Kumar Karki",
                "Devka Adhikari",
                "Grisma Karki",
            ],
        },
        "article": {
            "url": (
                "https://ekantipur.com/en/news/2025/04/21/33-million-corruption-case-against-amin-of-valley-development-authority-51-04.html"
                ),
            "title": (
                "3.3 million corruption case against Amin of Valley Development Authority - "
                "कान्तिपुर"
                ),
            "text": (
                "A corruption case has been filed against Amin Dinesh Prasad Yadav of the "
                "Kathmandu Valley Development Authority, claiming a sum of Rs. The Abuse of "
                "Authority Investigation Commission filed a case in a special court on Monday "
                "against Amin Yadav, who is working on a contract in Gaurighat under the "
                "Bagmati Nagar Land Consolidation Project run by the Valley Development "
                "Authority, District Commissioner's Office, Kathmandu, and charged Rs. "
                "According to the authority, a case has been filed with the demand of fine "
                "and imprisonment as per the law after the investigation confirmed that Yadav "
                "had acquired illegal wealth by misusing the position of . Against him, the "
                "authority had brought the movable property acquired during the period from "
                "August 8, 2063 to June 30, 2081 under the scope of investigation. The source "
                "of undisclosed property has been confiscated by the authority. During that "
                "period, Y"
                ),
            "published": None,
        },
        "label": "no_match",
        "why": (
            "HARD, same scheme DIFFERENT FISCAL YEAR, near-identical amount: Rs 3.21cr FY080 "
            "case offered the Rs 3.29cr FY081 article. Both are illegal assets at the Bagmati "
            "Nagar land project."
            ),
        "article_from": "081-CR-0076",
    },
    {
        "case": {
            "slug": "case-080-cr-0151-krishna-subedi-embezzlement",
            "court_case_no": "080-CR-0151",
            "title": (
                "श्री सदाशिव प्राथमिक विद्यालय, कास्कीकोट: प्रधानाध्यापक कृष्णप्रसाद सुवेदी "
                "लगायतले आम्दानी–खर्च लुकाई रु. ३.७७ लाख हिनामिना गरेको भ्रष्टाचार मुद्दा "
                "(080-CR-0151)"
                ),
            "short_description": (
                "कास्कीकोटस्थित श्री सदाशिव प्राथमिक विद्यालयका तत्कालीन प्रधानाध्यापक "
                "कृष्णप्रसाद सुवेदीले आम्दानी–खर्च विवरण र लेखापरीक्षण प्रतिवेदनमा गलत विवरण "
                "राखी विद्यालयको रु. ३.७७ लाखभन्दा बढी हिनामिना गरेको आरोप।"
                ),
            "key_allegations": [
                (
                    "श्री सदाशिव प्राथमिक विद्यालय, कास्कीकोटका तत्कालीन प्रधानाध्यापक कृष्ण "
                    "प्रसाद सुवेदीले विद्यालयको आ.व. २०६८/६९ र २०६९/७० को आम्दानी–खर्च "
                    "विवरणमा जिल्ला शिक्षा कार्यालयबाट निकासा भएको रकम आयमा नदेखाई, नभएको "
                    "आम्दानी र खर्च लेखापरीक्षण प्रतिवेदनमा प्रविष्ट गराई तथा भवन निर्माण "
                    "समितिको निर्णयमा आफूखुसी थपघट गरी विद्यालयको रु. ३ लाख ७७ हजारभन्दा बढी "
                    "रकम हानिनोक्सानी पुर्‍याएको।"
                    ),
                (
                    "तत्कालीन प्रधानाध्यापक सुवेदीले खर्च पुष्ट्याउने बिल भरपाई बिना नभएको "
                    "कामलाई खर्च देखाई विद्युतीय माध्यमबाट तयार लेखापरीक्षण प्रतिवेदनमा गलत "
                    "विवरण समावेश गरेको।"
                    ),
                (
                    "तत्कालीन लेखापरीक्षक कृष्ण प्रसाद गौतमले विद्यालयको वास्तविक "
                    "आम्दानी–खर्चसँग नमिल्ने लेखापरीक्षण प्रतिवेदन तयार गरी गलत विवरण "
                    "प्रमाणित गर्न सहयोग गरेको।"
                    ),
            ],
            "accused": [
                "Krishna Prasad Subedi",
                "Krishna Prasad Gautam",
            ],
        },
        "article": {
            "url": (
                "https://english.onlinekhabar.com/nac-wide-body-controversy-rs-1-47-billion-corruption-in-wide-body-32-defendants-including-ex-minister-shahi.html"
                ),
            "title": (
                "NAC wide-body controversy: Rs 1.47 billion corruption in wide-body, 32 "
                "defendants including ex-minister Shahi - OnlineKhabar English News"
                ),
            "text": (
                "The Commission for Investigation of Abuse of Authority (CIAA) on Thursday "
                "lodged a charge sheet against 32 persons, including former Minister for "
                "Culture, Tourism and Civil Aviation Jeevan Bahadur Shahi, on the charge of "
                "corruption in the procurement of wide-body aircraft for the national flag "
                "carrier- Nepal Airlines Corporation (NAC). Other officials facing the charge "
                "sheet include then General Manager of NAC Sugat Ratna Kansakar, the "
                "government secretary and Chairperson of NAC Board of Directors Shankar "
                "Prasad Adhikari, then Director General of Customs Department and then NAC "
                "Board of Directors Shishir Kumar Dhungana, Civil Aviation Ministry’s Joint "
                "Secretary Buddhi Sagar Lamichhane and others. The CIAA has sought Rs 1.471 "
                "million in recovery for their alleged involvement in the misappropriation in "
                "the procurement of the wide-body aircraft. Similarly, others implicated in "
                "the case are "
                ),
            "published": None,
        },
        "label": "no_match",
        "why": (
            "EASY: a Rs 3.77 LAKH village school case offered the Rs 1.47 ARBA "
            "national-airline procurement article."
            ),
        "article_from": "080-CR-0145",
    },
    {
        "case": {
            "slug": "case-080-cr-0064-anup-mehra-land-fraud",
            "court_case_no": "080-CR-0064",
            "title": (
                "नवलपरासी हदबन्दी जग्गा प्रकरण: नक्कली हकवाला खडा गरी रु. ५ करोड बढीको सरकारी "
                "जग्गा हडपेको आरोपमा भारतीय नागरिक अनुप मेहरा विरुद्ध भ्रष्टाचार मुद्दा "
                "(080-CR-0064)"
                ),
            "short_description": (
                "हदबन्दीका कारण नेपाल सरकारको नाममा आउनुपर्ने नवलपरासीका जग्गा कर्मचारीसँगको "
                "मिलेमतोमा नक्कली हकवाला खडा गरी आफ्नो नाममा बिक्री गराई रु. ५ करोड ५ लाख "
                "बराबरको सरकारी सम्पत्ति हडपेको आरोपमा अनुप मेहराविरुद्ध दायर मुद्दा।"
                ),
            "key_allegations": [
                (
                    "भारतीय नागरिक अनुप मेहराले हदबन्दीका कारण नेपाल सरकारको नाममा आउनुपर्ने "
                    "नवलपरासीका जग्गा सरकारी कर्मचारीसँगको मिलेमतोमा गलत कागजात र गैरकानूनी "
                    "निर्णयमार्फत आफ्नो नामबाट बिक्री वितरण गराई लिखत मूल्यअनुसार रु. ५ करोड "
                    "५ लाख ४२ हजार सार्वजनिक सम्पत्ति हानि नोक्सानी गराएको।"
                    ),
                (
                    "अनुप मेहराले भूमिसुधार कार्यालय नवलपरासीका तत्कालीन भूमिसुधार अधिकारीलाई "
                    "प्रलोभनमा पारी कानूनविपरीत तीन परिवार संख्या कायम गराउने र हदबन्दी "
                    "रोक्का फुकुवा गराउने निर्णय गराएको।"
                    ),
                (
                    "अनुप मेहराले स्थानीय निकायका पदाधिकारीलाई प्रलोभनमा पारी भारतमा बसोवास "
                    "गर्ने व्यक्तिलाई नेपालमै बसोवास र मृत्यु भएको भनी नाता प्रमाणित तथा "
                    "मृत्यु दर्ताजस्ता गलत कागजात तयार गराई हकवालाभन्दा फरक व्यक्तिबाट जग्गा "
                    "बिक्री गराएको।"
                    ),
            ],
            "accused": [
                "Anup Mehra",
            ],
        },
        "article": {
            "url": "https://english.ratopati.com/story/33976",
            "title": (
                "Final hearing begins in Melamchi Water Project corruption case | Ratopati | "
                "No.1 Nepali News Portal"
                ),
            "text": (
                "# Final hearing begins in Melamchi Water Project corruption case Kathmandu, "
                "August 26 — The final hearing has commenced in the case filed by the "
                "Commission for Investigation of Abuse of Authority (CIAA), which alleges "
                "corruption in the national pride Melamchi drinking water project. The bench, "
                "comprising Special Court Chairman Teknarayan Kunwar, Tejnarayan Singh Rai, "
                "and Murari Babu Shrestha, began the final hearing on Sunday. The CIAA filed "
                "a corruption case against 14 individuals on February 18, 2024, including "
                "three former secretaries, as well as consultants and construction companies, "
                "accusing them of financial irregularities due to repeatedly extending the "
                "project's deadline. The indictment claims that payments were improperly made "
                "in the project and that the accused were involved in this process. It "
                "alleges that construction professionals were paid without performing any "
                "work and t"
                ),
            "published": None,
        },
        "label": "no_match",
        "why": "EASY: Nawalparasi land-ceiling fraud offered the Melamchi water project article.",
        "article_from": "080-CR-0106",
    },
    {
        "case": {
            "slug": "case-080-cr-0190-bharat-tal-scam",
            "court_case_no": "080-CR-0190",
            "title": (
                "भरत ताल घोटाला: बागमती नगरपालिकाका निलम्बित नगर प्रमुख भरत कुमार थापा लगायत "
                "विरुद्ध नदीजन्य पदार्थ उत्खननमा रु. ३०.२९ करोड बिगोको भ्रष्टाचार मुद्दा "
                "(080-CR-0190)"
                ),
            "short_description": (
                "बागमती नगरपालिकाका निलम्बित नगर प्रमुख भरत कुमार थापा लगायतले सागरनाथ वन "
                "क्षेत्रमा स्वीकृतिबिना माछा पोखरी निर्माणका नाममा नदीजन्य पदार्थ उत्खनन गरी "
                "रु. ३०.२९ करोडको सार्वजनिक सम्पत्ति हानि पुर्‍याएको आरोप।"
                ),
            "key_allegations": [
                (
                    "बागमती नगरपालिकाका हाल निलम्बित नगर प्रमुख भरत कुमार थापा, नगर उपप्रमुख "
                    "लीला कुमारी मुक्तान, तत्कालीन प्रमुख प्रशासकीय अधिकृत बिमल कुमार "
                    "पोखरेलसहितका पदाधिकारीहरूले सागरनाथ वन विकास परियोजनाअन्तर्गतको वन "
                    "क्षेत्रमा रहेको बागमती माछा पोखरी प्रथम र दोस्रोबाट नेपाल सरकार तथा "
                    "परियोजनाको स्वीकृतिबिना आ.व. २०७५/०७६ देखि निरन्तर नदीजन्य वन पैदावार "
                    "उत्खनन् गरी नपुग देखिएको १३ लाख ४० हजार घनमिटरभन्दा बढी ढुंगा, गिटी, "
                    "बालुवा दुरुपयोग गराई VAT सहित रु. ३० करोडभन्दा बढी सार्वजनिक सम्पत्ति "
                    "हानि पुर्‍याएको।"
                    ),
                (
                    "नगरपालिकाका पदाधिकारीहरूले माछा पोखरी निर्माणका लागि प्रारम्भिक "
                    "वातावरणीय परीक्षण प्रतिवेदन स्वीकृत भएको भनी गलत लिखत तयार गराई अनधिकृत "
                    "उत्खननलाई वैध देखाउने आधार बनाएको।"
                    ),
                (
                    "बागमती नगरपालिकाका प्राविधिक, योजना र लेखा भूमिकाका कर्मचारीहरूले "
                    "गुरुयोजना स्वीकृत नगरी, DPR तयार भएको देखाई र बागमती नदीको बहाव प्रयोगको "
                    "स्वीकृति नलिई वन क्षेत्रमा माछा पोखरी निर्माण तथा नदीजन्य पदार्थ उत्खनन "
                    "प्रक्रियामा सहयोग गरेको।"
                    ),
            ],
            "accused": [
                "Methur Chaudhri",
                "Sagar Poudel",
                "Bishwaraj Pokharel",
                "Bimal Kumar Pokharel",
                "Lila Kumari Muknat",
                "Bharat Kumar Thapa",
            ],
        },
        "article": {
            "url": (
                "https://thehimalayantimes.com/nepal/government-official-held-for-accumulating-illegal-property-worth-rs-46mn"
                ),
            "title": (
                "Government official held for accumulating illegal property worth Rs 46mn - "
                "The Himalayan Times - Nepal's No.1 English Daily Newspaper | Nepal News, "
                "Latest Politics, Business, World, Sports, Entertainment, Travel, Life Style "
                "News"
                ),
            "text": (
                "**KATHMANDU, SEPTEMBER 9** The Commission for the Investigation of Abuse of "
                "Authority has filed a chargesheet at the Special Court against Toran Bahadur "
                "Karki, account officer of Lalitpur-based Water Resources Research "
                "Development Centre, for allegedly amassing disproportionate assets worth "
                "around Rs 46 million. The CIAA had launched a thorough investigation into "
                "his property status and income source after it received a complaint that he "
                "had accummulated illegal assets. The anti-graft body said Karki managed to "
                "establish the income source of only Rs 11 million of the assets, both "
                "moveable and immovable, worth around Rs 57.5 million he had claimed to have "
                "earned after joining government office on 30 May 1996. A press release "
                "issued by the CIAA claimed that Karki was found to have accumulated illegal "
                "assets of around Rs 46 million through corruption and financial "
                "irregularities till last f"
                ),
            "published": None,
        },
        "label": "no_match",
        "why": (
            "EASY: the Bharat Tal riverbed-extraction scam offered an individual "
            "illegal-assets article."
            ),
        "article_from": "080-CR-0032",
    },
    {
        "case": {
            "slug": "case-080-cr-0085-dirgha-koirala-illegal-assets",
            "court_case_no": "080-CR-0085",
            "title": (
                "डिभिजन वन अधिकृत दीर्घ नारायण कोईराला: रु.१ करोड २६ लाख स्रोत नखुलेको "
                "सम्पत्ति आर्जन (080-CR-0085)"
                ),
            "short_description": (
                "मकवानपुरका डिभिजनल वन अधिकृत दीर्घ नारायण कोईरालाले वैध आयभन्दा करिब रु.१.२६ "
                "करोड बढी स्रोत नखुलेको सम्पत्ति आर्जन गरी श्रीमतीको नाममा जग्गा-घरमा लगानी "
                "गरेको आरोप।"
                ),
            "key_allegations": [
                (
                    "डिभिजन वन कार्यालय, मकवानपुरका डिभिजनल वन अधिकृत दीर्घ नारायण कोईरालाले "
                    "सार्वजनिक सेवामा स्थायी नियुक्ति लिएको मिति २०४६/०२/०२ देखि २०७८/११/१२ "
                    "सम्म वैध आय रु. ८१ लाखभन्दा बढी मात्र हुँदा घर निर्माण, जग्गा खरिद, "
                    "सवारी खरिद, शेयर लगानी, ग्यास स्टोर लगानी र बैंक मौज्दातलगायतमा रु. २ "
                    "करोड ७ लाखभन्दा बढी खर्च तथा लगानी गरी आयभन्दा बढी करिब रु. १ करोड २५ "
                    "लाख ९६ हजार बराबर स्रोत नखुलेको सम्पत्ति आर्जन गरेको।"
                    ),
                (
                    "दीर्घ नारायण कोईरालाले जाँच अवधिमा पारिश्रमिक, भत्ता, घरजग्गा बिक्री, "
                    "बीमा परिपक्व रकम, ग्यास स्टोर, शेयर बिक्री, कृषि आय र छोरीको बचतसमेतबाट "
                    "देखिएको वैध आम्दानीभन्दा धेरै बढी सम्पत्ति सिर्जना गरी सम्पत्ति विवरण "
                    "अमिल्दो र अस्वाभाविक बनाएको।"
                    ),
                (
                    "दीर्घ नारायण कोईरालाले सार्वजनिक पदमा रहँदा आर्जन गरेको स्रोत नखुलेको "
                    "सम्पत्ति श्रीमती गीता पौडेल कोईरालाको नाममा जग्गा खरिद, सो जग्गामा घर "
                    "निर्माण र बैंक खातामा रकम मौज्दात राख्न प्रयोग गरेको।"
                    ),
            ],
            "accused": [
                "Deergh Narayan Koirala",
                "Gita Koirala Poudel",
            ],
        },
        "article": {
            "url": (
                "https://myrepublica.nagariknetwork.com/news/former-nta-chairs-jha-khanal-convicted-in-mdms-procurement-embezzlement-19-80.html"
                ),
            "title": (
                "Former NTA Chairs Jha, Khanal convicted in MDMS procurement embezzlement - "
                "myRepublica - The New York Times Partner, Latest news of Nepal in English, "
                "Latest News Articles | Republica"
                ),
            "text": (
                "**Other employees acquitted ** KATHMANDU, March 7: The Special Court "
                "convicted two former chairpersons of the Nepal Telecommunications Authority "
                "(NTA) in a corruption case related to the procurement of the Mobile Device "
                "Management System (MDMS) but acquitted the other accused employees. On "
                "Thursday, a joint bench of Judges Tek Narayan Kunwar, Ritesh Thapa, and "
                "Bidur Koirala found former chairpersons Digambar Jha and Purushottam Prasad "
                "Khanal guilty in the embezzlement case. The court ruled that they committed "
                "offenses under Section 8, Subsection (1), Clauses (a), (d), and (j) of the "
                "Corruption Prevention Act, 2002. Additionally, the court convicted the "
                "company that secured the contract. ### MDMS to be fully implemented from "
                "today, entrepreneurs expect a... The Special Court ordered a one-year prison "
                "sentence and a fine of Rs 58.15 million each for the former chairpersons, "
                "matching the em"
                ),
            "published": None,
        },
        "label": "no_match",
        "why": (
            "EASY: a divisional forest officer's Rs 1.26cr illegal assets offered the NTA "
            "telecom MDMS procurement article."
            ),
        "article_from": "080-CR-0141",
    },
    {
        "case": {
            "slug": "case-080-cr-0032-toran-karki-illegal-assets",
            "court_case_no": "080-CR-0032",
            "title": (
                "जलस्रोत अनुसन्धान विकास केन्द्रका लेखा अधिकृत तोरण बहादुर कार्कीले रु.४.६७ "
                "करोड स्रोत नखुलेको सम्पत्ति आर्जन गरेको आरोप (080-CR-0032)"
                ),
            "short_description": (
                "जलस्रोत अनुसन्धान विकास केन्द्र, पुल्चोकका लेखा अधिकृत तोरण बहादुर कार्कीले "
                "वैध आयभन्दा बढी खर्च-लगानी गरी करिब रु.४.६७ करोड स्रोत नखुलेको सम्पत्ति "
                "आर्जन गरी श्रीमतीसमेतको नाममा लुकाएको आरोप।"
                ),
            "key_allegations": [
                (
                    "जलस्रोत अनुसन्धान विकास केन्द्र, पुल्चोकका लेखा अधिकृत तोरण बहादुर "
                    "कार्कीले सार्वजनिक सेवा प्रवेश गरेको मिति २०५३।०२।१७ देखि २०७९।०८।२८ "
                    "सम्मको जाँच अवधिमा वैध आय रु. १ करोड ८ लाखभन्दा बढी हुँदा पनि घर "
                    "निर्माण, जग्गा खरिद, शेयर लगानी, सवारी साधन खरिद र बैंक मौज्दात लगायतमा "
                    "रु. ५ करोड ७५ लाखभन्दा बढी खर्च तथा लगानी गरी करिब रु. ४ करोड ६७ लाख "
                    "स्रोत नखुलेको सम्पत्ति आर्जन गरेको।"
                    ),
                (
                    "तोरण बहादुर कार्कीले वैध आयसँग नमिल्ने खर्च तथा लगानीबाट आफू र परिवारको "
                    "अमिल्दो तथा अस्वाभाविक उच्चस्तरको जीवनयापन कायम गरेको।"
                    ),
                (
                    "तोरण बहादुर कार्कीले स्रोत नखुलेको सम्पत्ति श्रीमती उर्मिला थापा "
                    "कार्कीसमेतको नाममा जग्गा, शेयर, सवारी साधन र बैंक मौज्दातमा राखी "
                    "सम्पत्ति लुकाउने वा स्थानान्तरण गर्ने काम गरेको।"
                    ),
            ],
            "accused": [
                "Toran Bahadur Karki",
                "Urmila Thapa Karki",
            ],
        },
        "article": {
            "url": "https://example-news.test/patan-high-court-080-cr-0032-tenancy",
            "title": "Patan High Court concludes tenancy appeal 080-CR-0032",
            "text": (
                "LALITPUR — The Patan High Court on Sunday concluded hearing case "
                "080-CR-0032, a tenancy appeal filed by Ram Prasad Shrestha against a "
                "Lalitpur landlord over a disputed rental agreement. A single bench of Judge "
                "Nirmala Devi Rai reserved judgment. The appellant's counsel argued the "
                "district court had misread the tenancy act; the respondent said the "
                "agreement had lapsed. The court will announce its decision next month. The "
                "registry said case 080-CR-0032 was registered at the Patan High Court in "
                "Bhadra and is unrelated to any corruption proceeding."
                ),
            "published": "2024-09-15",
        },
        "label": "no_match",
        "why": (
            "HARD, same case NUMBER different court, SYNTHETIC: 080-CR-0032 at the Patan High "
            "Court is a tenancy appeal, not this CIAA case. Prod has no real instance -- "
            "every batch case is Special Court."
            ),
        "article_from": "080-CR-0032 (Patan High Court, synthetic)",
    },
]

MATCHES = [p for p in LABELLED_PAIRS if p["label"] == "match"]
NON_MATCHES = [p for p in LABELLED_PAIRS if p["label"] == "no_match"]
HARD_NEGATIVES = [p for p in NON_MATCHES if p["why"].startswith("HARD")]
