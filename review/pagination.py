from rest_framework.pagination import PageNumberPagination


class ReviewResultsPagination(PageNumberPagination):
    """Page-number pagination for the review list.

    Inherits the project default page size (settings PAGE_SIZE) but lets the
    client request a page size via ``?page_size=`` so the SPA can lazy-load the
    list (infinite scroll) in chunks, capped by ``max_page_size`` to keep
    responses bounded.
    """

    page_size_query_param = "page_size"
    max_page_size = 100
