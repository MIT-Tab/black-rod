from haystack import indexes

from core.models.debater import Debater


class DebaterIndex(indexes.SearchIndex, indexes.Indexable):
    text = indexes.CharField(document=True, use_template=True)
    name = indexes.CharField(model_attr='name')

    name_auto = indexes.EdgeNgramField(model_attr='name')

    def get_model(self):
        return Debater
