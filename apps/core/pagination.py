from django.core.paginator import Paginator


PAGE_SIZES = ("10", "20", "all")


def paginate(request, items):
    page_size = request.GET.get("page_size", "10")
    if page_size not in PAGE_SIZES:
        page_size = "10"
    params = request.GET.copy()
    params.pop("page", None)
    params.pop("page_size", None)
    query_params = params.urlencode()
    if page_size == "all":
        return items, None, page_size, query_params
    page_obj = Paginator(items, int(page_size)).get_page(request.GET.get("page", 1))
    return page_obj.object_list, page_obj, page_size, query_params
