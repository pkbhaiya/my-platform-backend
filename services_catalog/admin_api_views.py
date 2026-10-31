from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from orders.services import live_capacity_limit

from .models import Service
from .serializers import AdminServiceSerializer


class AdminServicePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class AdminServiceListCreateAPIView(APIView):
    permission_classes = [IsAdminUser]
    pagination_class = AdminServicePagination

    def get(self, request):
        queryset = Service.objects.all().order_by("service_id")
        search = request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(Q(name__icontains=search) | Q(description__icontains=search))
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(AdminServiceSerializer(page, many=True).data)

    def post(self, request):
        serializer = AdminServiceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        service = serializer.save()
        return Response(AdminServiceSerializer(service).data, status=status.HTTP_201_CREATED)


class AdminServiceDetailAPIView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, service_id):
        service = get_object_or_404(Service, pk=service_id)
        # Guidance for the form: orders above this are refused today, whatever the stable maximum.
        return Response({**AdminServiceSerializer(service).data, "live_max_order_quantity": live_capacity_limit(service)})

    def patch(self, request, service_id):
        service = get_object_or_404(Service, pk=service_id)
        serializer = AdminServiceSerializer(service, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return Response(AdminServiceSerializer(serializer.save()).data)
