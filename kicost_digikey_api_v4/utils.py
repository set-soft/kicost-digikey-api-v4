# -*- coding: utf-8 -*-
# GPL license
#
# Copyright (C) 2021 by Salvador E. Tropea / Instituto Nacional de Tecnologia Industrial
#
from copy import copy
import os
import re
import logging

import kicost_digikey_api_v4
from kicost_digikey_api_v4.v4.productinformation import KeywordRequest, FilterOptionsRequest
# ManufacturerProductDetailsRequest, no longer in V4
from .exceptions import DigikeyError

USER_AGENT = "Mozilla/5.0 (Windows NT 6.2; rv:22.0) Gecko/20130405 Firefox/22.0"
includes = ["DigiKeyPartNumber","ProductUrl","QuantityAvailable","MinimumOrderQuantity","PrimaryDatasheet","ProductStatus",
            "SearchLocaleUsed","StandardPricing","Parameters","RoHsStatus","AdditionalValueFee","ProductDescription"]
includes = ','.join(includes)


class PartSortWrapper(object):
    """ This class is used to sort the results giving more priority to entries with less MOQ, less price,
        more availability, etc. """
    def __init__(self, data, status, parent):
        self.data = data
        self.min_price = data.standard_pricing[0].unit_price if len(data.standard_pricing) > 0 else -1
        self.status = status
        self.parent = parent

    def __eq__(self, other):
        return (self.data.minimum_order_quantity == other.data.minimum_order_quantity and
                self.data.quantity_availablefor_package_type == other.data.quantity_availablefor_package_type and
                self.data.digi_reel_fee == other.data.digi_reel_fee and
                self.min_price == other.min_price and
                self.status == other.status)

    def __lt__(self, other):
        if self.data.quantity_availablefor_package_type and not other.data.quantity_availablefor_package_type:
            return True
        if not self.data.minimum_order_quantity:
            return False
        if self.data.minimum_order_quantity < other.data.minimum_order_quantity:
            return True
        if self.min_price == -1:
            return False
        dif = self.data.digi_reel_fee + self.min_price - (other.data.digi_reel_fee + other.min_price)
        if dif < 0:
            return True
        if dif == 0 and self.status == 'Active' and other.status != 'Active':
            return True
        return False


class DK_API(object):
    ''' Configuration class, KiCost must extend it and provide an object with the desired options '''
    # Provided by KiCost
    id = secret = None
    sandbox = False
    api_ops = {}
    exclude_market_place_products = False
    # Configured here
    cache = None
    logger = logging.getLogger(__name__)
    extra_ops = {}  # Extra options for the API

    @staticmethod
    def _create_cache_name_suffix():
        suf = '_' + DK_API.extra_ops.get('x_digikey_locale_site', 'US')
        suf += '_' + DK_API.extra_ops.get('x_digikey_locale_language', 'en')
        suf += '_' + DK_API.extra_ops.get('x_digikey_locale_currency', 'USD')
        suf += '_' + DK_API.extra_ops.get('x_digikey_locale_ship_to_country', 'US')
        suf += '_v4'
        return suf

    @staticmethod
    def configure(cache, a_logger=None):
        ''' Configures the plug-in '''
        if a_logger:
            DK_API.logger = a_logger
            kicost_digikey_api_v4.v4.api.set_logger(a_logger)
            kicost_digikey_api_v4.oauth.oauth2.set_logger(a_logger)
        # Ensure we have a place to store the token
        DK_API.cache = cache
        cache_path = cache.path
        if not os.path.isdir(cache_path):
            raise DigikeyError("No directory to store tokens, please create `{}`".format(cache_path))
        os.environ['DIGIKEY_STORAGE_PATH'] = cache_path
        # Ensure we have the credentials
        if not DK_API.id or not DK_API.secret:
            raise DigikeyError("No Digi-Key credentials defined")
        os.environ['DIGIKEY_CLIENT_ID'] = DK_API.id
        os.environ['DIGIKEY_CLIENT_SECRET'] = DK_API.secret
        # Default to no sandbox
        os.environ['DIGIKEY_CLIENT_SANDBOX'] = str(DK_API.sandbox)
        # API options
        DK_API.extra_ops = {'x_digikey_'+op: val for op, val in DK_API.api_ops.items()}
        # Cache suffix (uses extra_ops)
        cache.suffix = DK_API._create_cache_name_suffix()
        # Debug information about what we got
        DK_API.logger.debug('Digi-Key API plug-in options:')
        DK_API.logger.debug(str([k + '=' + v for k, v in os.environ.items() if k.startswith('DIGIKEY_')]))
        DK_API.logger.debug(str(DK_API.extra_ops))


class by_digikey_pn(object):
    def __init__(self, dk_pn):
        self.dk_pn = dk_pn

    def search(self):
        self.api_limit = {}
        result, loaded = DK_API.cache.load_results('dpn', self.dk_pn)
        if not loaded:
            result = kicost_digikey_api_v4.product_details(self.dk_pn, api_limits=self.api_limit, **DK_API.extra_ops)
            DK_API.cache.save_results('dpn', self.dk_pn, result)
        if result:
            # This query returns a family of results, but we want just one, the one indicated by the user
            for v in result.product.product_variations:
                if v.digi_key_product_number == self.dk_pn:
                    result.product.match = v
                    return result
        return None


class by_manf_pn(object):
    def __init__(self, dk_pn):
        self.dk_pn = dk_pn

    def search(self):
        self.api_limit = {}
        result, loaded = DK_API.cache.load_results('dpn', self.dk_pn)
        if not loaded:
            result = kicost_digikey_api_v4.product_details(self.dk_pn, api_limits=self.api_limit, **DK_API.extra_ops)
            DK_API.cache.save_results('dpn', self.dk_pn, result)
        if not result:
            return None
        product = result.product
        res = [product]
        for v in result.product.product_variations[1:]:
            res.append(copy(product))
        # This query returns a family of results, here we want all, expand them
        for c, v in enumerate(product.product_variations):
            res[c].match = v
        result.products = res
        # Now choose one
        tmp_results = [PartSortWrapper(p, product.product_status.status, product) for p in product.product_variations]
        tmp_results.sort()
        product.match = tmp_results[0].data
        return result


class by_keyword(object):
    def __init__(self, keyword):
        self.keyword = keyword

    def search(self):
        market_place = 'ExcludeMarketPlace' if DK_API.exclude_market_place_products else 'NoFilter'
        search_request = KeywordRequest(keywords=self.keyword, limit=10,
                                        filter_options_request=FilterOptionsRequest(market_place_filter=market_place))
        self.api_limit = {}
        result, loaded = DK_API.cache.load_results('key', self.keyword)
        if not loaded:
            result = kicost_digikey_api_v4.keyword_search(body=search_request, api_limits=self.api_limit, **DK_API.extra_ops) #, includes=includes)
            DK_API.cache.save_results('key', self.keyword, result)
        if not result:
            return None
        products = result.products
        count = result.products_count
        # print(products)
        if count == 0:
            return None
        # Choose one
        tmp_results = []
        for r in products:
            for p in r.product_variations:
                tmp_results.append(PartSortWrapper(p, r.product_status.status, r))
        tmp_results.sort()
        result.product = tmp_results[0].parent
        result.product.match = tmp_results[0].data
        if False:
            print('* ' + self.keyword + ':')
            for rs in tmp_results:
                r = rs.data
                print('- {} {} {} {}'.format(r.digi_key_product_number, r.minimum_order_quantity, rs.min_price, r.digi_reel_fee))
        return result
