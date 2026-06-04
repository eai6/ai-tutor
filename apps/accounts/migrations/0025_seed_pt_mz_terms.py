"""Seed a Portuguese (Mozambique) PlatformTerms row alongside the existing
English one, so the consent text renders in the student's UI language.

Runs on every database: on Seychelles/prod it simply adds a pt-mz row that
en-us users never see (active() picks by locale); on the Mozambique pilot
it becomes the terms shown at sign-up. Idempotent and reversible.
"""
from django.db import migrations


PT_TITLE = "ExplicadorMoz — Termos e Notas Importantes"

PT_SUMMARY = (
    "Compreendo que o ExplicadorMoz usa modelos de IA que podem "
    "ocasionalmente comportar-se de forma inesperada, que os alunos devem "
    "ser supervisionados por um adulto e que a plataforma é fornecida no "
    "estado em que se encontra."
)

PT_BODY = """Ao usar o ExplicadorMoz, concordas com o seguinte.

## 1. Comportamento e exatidão da IA
O ExplicadorMoz usa modelos de linguagem de grande escala para ensinar. Os sistemas de IA podem produzir respostas incorretas, inesperadas ou fora do tema. Apesar das medidas de segurança que implementámos, a IA pode, em casos raros, produzir conteúdo que contorna as suas salvaguardas. Não garantimos a correção, a exatidão ou a adequação de todas as respostas geradas pela IA. Trata as explicações da IA como um ponto de partida, não como uma fonte final de verdade.

## 2. É esperada a supervisão de um adulto
Esta plataforma foi concebida para ser usada por alunos sob a supervisão de um adulto — um professor, pai/mãe ou encarregado de educação. Os alunos não devem ser deixados sem supervisão na plataforma durante períodos prolongados. As escolas, os pais e os encarregados de educação são responsáveis por monitorizar o uso da plataforma pelos seus alunos e por intervir se algo parecer inadequado.

## 3. Responsabilidade
Disponibilizamos o ExplicadorMoz de boa-fé para fins educativos. Na medida do permitido por lei, não somos responsáveis por quaisquer perdas, danos ou prejuízos decorrentes do comportamento da IA, do conteúdo apresentado pela plataforma ou da forma como os alunos a utilizam. Ao continuar a usar a plataforma, aceitas este risco relacionado com a IA em teu nome e (quando aplicável) em nome do aluno que supervisionas.

## 4. Dados que recolhemos
Recolhemos e armazenamos informações da conta, progresso das lições, transcrições das conversas e resultados das avaliações. Usamos estes dados para operar a plataforma, adaptar o explicador a cada aluno e reportar o progresso à escola do aluno. Não vendemos dados dos alunos. O pessoal da escola e os administradores da plataforma podem ver os teus dados de utilização relativos à sua escola.

## 5. Comunicar preocupações
Se a IA disser algo inadequado, clica no ícone de bandeira por baixo da mensagem e informa o teu professor ou o adulto responsável. Também podes usar o botão de Ajuda / Feedback no fundo de cada página para nos comunicar erros e sugestões diretamente.

## 6. Atualizações a estes termos
Podemos atualizar estes termos. Quando a versão mudar, ser-te-á pedido que concordes novamente no próximo início de sessão.

Ao clicares em "Concordo", confirmas que leste e aceitaste estes termos — e, quando aplicável, que o adulto responsável (professor, pai/mãe ou encarregado de educação) os reviu.
"""


def seed_pt_mz_terms(apps, schema_editor):
    PlatformTerms = apps.get_model('accounts', 'PlatformTerms')
    # Belt-and-braces: tag any untagged row as en-us (the AddField default
    # already does this for rows that existed before this migration).
    PlatformTerms.objects.filter(locale='').update(locale='en-us')

    # Mirror the active English version number so re-acceptance tracking
    # (terms_accepted_version) stays parallel across locales.
    active_en = (
        PlatformTerms.objects.filter(is_active=True, locale='en-us')
        .order_by('-version').first()
    )
    version = active_en.version if active_en else 1

    # Idempotent — don't duplicate on re-run.
    if PlatformTerms.objects.filter(version=version, locale='pt-mz').exists():
        return

    PlatformTerms.objects.create(
        version=version,
        locale='pt-mz',
        is_active=True,
        title=PT_TITLE,
        summary=PT_SUMMARY,
        body=PT_BODY,
        effective_date=active_en.effective_date if active_en else None,
    )


def remove_pt_mz_terms(apps, schema_editor):
    PlatformTerms = apps.get_model('accounts', 'PlatformTerms')
    PlatformTerms.objects.filter(locale='pt-mz').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0024_platformterms_locale'),
    ]

    operations = [
        migrations.RunPython(seed_pt_mz_terms, remove_pt_mz_terms),
    ]
