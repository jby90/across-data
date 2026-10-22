from app.ingestion.parsing import text_layer_garbled

EN = (
    "The Commission may, on the application of any organisation, by written notice exempt the "
    "organisation from any requirement prescribed pursuant to this Act in respect of any transfer "
    "of personal data by that organisation. An exemption under this section may be granted subject "
    "to such conditions as the Commission may specify in writing and may be revoked at any time. "
) * 3
FR = (
    "Toute personne physique ou morale ou l'autorité publique ainsi que leurs subordonnés, à "
    "l'exception de la personne concernée, qui décide des finalités et des moyens du traitement "
    "des données à caractère personnel est le responsable du traitement pour les besoins de la loi. "
) * 3
AR = (
    "يجب على المسؤول عن المعالجة الحصول على ترخيص الهيئة في صورة حدوث أي تغيير على البيانات "
    "المشار إليها أعلاه ويقدم طلب الترخيص من قبل المسؤول عن المعالجة أو ممثله القانوني "
    "ولا يعفي الترخيص من المسؤولية إزاء الغير وتضبط شروط تقديم طلب الترخيص وإجراءاته بأمر "
) * 3
# 真实案例的形态:阿语 PDF 的文字层是字体编码缺失导致的控制字符与拉丁乱码
GARBLED_AR = (
    " \x15 _\x0fLc¼.7\x0f\x13d\x12 Óº0\x19_{º Î\n±\x0fgng\x0fW \x15\x13\x1fºn_º\nº,k fc \x15\x15 ^<T_ "
) * 8


def test_good_text_layers_pass():
    assert text_layer_garbled([EN], "en") is None
    assert text_layer_garbled([FR], "fr") is None
    assert text_layer_garbled([AR], "ar") is None


def test_real_world_garbled_arabic_layer_is_detected():
    reason = text_layer_garbled([GARBLED_AR], "ar")
    assert reason is not None and ("控制字符" in reason or "阿拉伯字母" in reason)


def test_latin_gibberish_in_english_document_is_detected():
    gibberish = "xq zvk pml wtr ghj bnd fcx qwe rty uio plk mjn bvc xsz " * 12
    assert text_layer_garbled([gibberish], "en") is not None


def test_wrong_script_is_detected():
    assert text_layer_garbled([EN], "ar") is not None  # 声明阿语,文字层却是拉丁字母
    assert text_layer_garbled([AR], "fr") is not None


def test_short_or_unknown_language_is_not_judged():
    assert text_layer_garbled(["short"], "en") is None
    assert text_layer_garbled([EN], "zh") is None  # 没有判定规则的语言不乱判
