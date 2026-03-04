"""Link unlinked teaching materials to matching courses."""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Link unlinked teaching materials to matching courses by subject"

    def handle(self, *args, **options):
        from apps.dashboard.material_tasks import link_unlinked_materials

        linked = link_unlinked_materials()
        self.stdout.write(self.style.SUCCESS(f"Linked {linked} materials"))
