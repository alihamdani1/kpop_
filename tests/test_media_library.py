from __future__ import annotations

from pathlib import Path

from kpop_bot import media_library


def _make_image(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"fake-image-bytes")
    return path


def test_select_images_utilise_le_dossier_du_groupe_en_priorite(tmp_path):
    _make_image(tmp_path / "BTS", "01.jpg")
    _make_image(tmp_path / "BTS", "02.jpg")
    _make_image(tmp_path / "_generic", "generic_01.jpg")

    images = media_library.select_images_for_thread(
        tmp_path, group_name="BTS", concept_id=None, count=2
    )
    assert len(images) == 2
    assert all(img.parent.name == "BTS" for img in images)


def test_select_images_repli_sur_sous_categorie_generic_si_concept_mappe(tmp_path):
    _make_image(tmp_path / "_generic" / "industrie", "industrie_01.jpg")
    _make_image(tmp_path / "_generic", "generic_flat_01.jpg")

    images = media_library.select_images_for_thread(
        tmp_path, group_name="Groupe Inconnu", concept_id="affaire_industrie", count=1
    )
    assert images[0].parent.name == "industrie"


def test_select_images_repli_sur_racine_generic_si_pas_de_sous_categorie(tmp_path):
    _make_image(tmp_path / "_generic", "generic_01.jpg")

    images = media_library.select_images_for_thread(
        tmp_path, group_name="Groupe Inconnu", concept_id="rivalite_generationnelle", count=1
    )
    assert images[0].parent.name == "_generic"


def test_select_images_repli_sur_racine_generic_si_sous_categorie_vide(tmp_path):
    (tmp_path / "_generic" / "industrie").mkdir(parents=True)  # existe mais vide
    _make_image(tmp_path / "_generic", "generic_01.jpg")

    images = media_library.select_images_for_thread(
        tmp_path, group_name="Groupe Inconnu", concept_id="affaire_industrie", count=1
    )
    assert images[0].parent.name == "_generic"


def test_select_images_liste_vide_si_rien_de_disponible(tmp_path):
    images = media_library.select_images_for_thread(
        tmp_path, group_name="Groupe Sans Photos", concept_id=None, count=3
    )
    assert images == []


def test_select_images_repete_si_le_pool_est_plus_petit_que_count(tmp_path):
    _make_image(tmp_path / "BTS", "01.jpg")
    _make_image(tmp_path / "BTS", "02.jpg")

    images = media_library.select_images_for_thread(
        tmp_path, group_name="BTS", concept_id=None, count=5
    )
    assert len(images) == 5
    assert all(img.parent.name == "BTS" for img in images)
    assert set(images) == {tmp_path / "BTS" / "01.jpg", tmp_path / "BTS" / "02.jpg"}


def test_select_images_sans_repetition_si_le_pool_est_assez_grand(tmp_path):
    for i in range(5):
        _make_image(tmp_path / "BTS", f"{i}.jpg")

    images = media_library.select_images_for_thread(
        tmp_path, group_name="BTS", concept_id=None, count=5
    )
    assert len(images) == len(set(images)) == 5


def test_select_images_ignore_les_fichiers_non_image(tmp_path):
    _make_image(tmp_path / "BTS", "photo.jpg")
    (tmp_path / "BTS" / "tags.yaml").write_text("photo.jpg: [stage]\n", encoding="utf-8")
    (tmp_path / "BTS" / ".gitkeep").write_text("", encoding="utf-8")

    images = media_library.select_images_for_thread(
        tmp_path, group_name="BTS", concept_id=None, count=1
    )
    assert images == [tmp_path / "BTS" / "photo.jpg"]


def test_select_images_count_zero_retourne_liste_vide(tmp_path):
    _make_image(tmp_path / "BTS", "01.jpg")
    assert (
        media_library.select_images_for_thread(tmp_path, group_name="BTS", concept_id=None, count=0)
        == []
    )
