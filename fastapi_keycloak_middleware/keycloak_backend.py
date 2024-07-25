"""
This module contains the Keycloak backend.

It is used by the middleware to perform the actual authentication.
"""

import logging
import typing

import keycloak
from keycloak import KeycloakOpenID
from starlette.authentication import AuthenticationBackend, BaseUser
from starlette.requests import HTTPConnection

from fastapi_keycloak_middleware.exceptions import (
    AuthClaimMissing,
    AuthHeaderMissing,
    AuthInvalidToken,
    AuthKeycloakError,
    AuthUserError,
)
from fastapi_keycloak_middleware.fast_api_user import FastApiUser
from fastapi_keycloak_middleware.schemas.authorization_methods import (
    AuthorizationMethod,
)
from fastapi_keycloak_middleware.schemas.keycloak_configuration import (
    KeycloakConfiguration,
)

log = logging.getLogger(__name__)


class KeycloakBackend(AuthenticationBackend):
    """
    Backend to perform authentication using Keycloak
    """

    def __init__(
        self,
        keycloak_configuration: KeycloakConfiguration,
        user_mapper: typing.Callable[[typing.Dict[str, typing.Any]], typing.Awaitable[typing.Any]],
    ):
        self.keycloak_configuration = keycloak_configuration
        self.keycloak_openid = self._get_keycloak_openid()
        self.get_user = user_mapper if user_mapper else KeycloakBackend._get_user

    def _get_keycloak_openid(self) -> KeycloakOpenID:
        """
        Instance-scoped KeycloakOpenID object
        """
        return KeycloakOpenID(
            server_url=self.keycloak_configuration.url,
            client_id=self.keycloak_configuration.client_id,
            realm_name=self.keycloak_configuration.realm,
            client_secret_key=self.keycloak_configuration.client_secret,
            verify=self.keycloak_configuration.verify,
        )

    @staticmethod
    async def _get_user(userinfo: typing.Dict[str, typing.Any]) -> BaseUser:
        """
        Default implementation of the get_user method.
        """
        return FastApiUser(
            first_name=userinfo.get("given_name", ""),
            last_name=userinfo.get("family_name", ""),
            user_id=userinfo.get("user_id", ""),
        )

    async def authenticate(self, conn: HTTPConnection) -> tuple[list[str], BaseUser | None]:
        """
        The authenticate method is invoked each time a route is called that
        the middleware is applied to.
        """

        # If this is a websocket connection, we can extract the token
        # from the cookies
        if (
            self.keycloak_configuration.enable_websocket_support
            and conn.headers.get("upgrade") == "websocket"
        ):
            auth_header = conn.cookies.get(self.keycloak_configuration.websocket_cookie_name, None)
        else:
            auth_header = conn.headers.get("Authorization", None)

        if not auth_header:
            raise AuthHeaderMissing

        # Check if token starts with the authentication scheme
        token = auth_header.split(" ")
        if len(token) != 2 or token[0] != self.keycloak_configuration.authentication_scheme:
            raise AuthInvalidToken

        # Depending on the chosen method by the user, either
        # use the introspection endpoint or decode the token
        if self.keycloak_configuration.use_introspection_endpoint:
            log.debug("Using introspection endpoint to validate token")
            # Call introspect endpoint to check if token is valid
            try:
                token_info = self.keycloak_openid.introspect(token[1])
            except keycloak.exceptions.KeycloakPostError as exc:
                raise AuthKeycloakError from exc
        else:
            log.debug("Using keycloak public key to validate token")
            # Decode Token locally using the public key
            token_info = self.keycloak_openid.decode_token(
                token[1],
                self.keycloak_configuration.validate_token,
                **self.keycloak_configuration.validation_options,
            )

        # Calculate claims to extract
        # Default is user configured claims
        claims = self.keycloak_configuration.claims
        # If device auth is enabled + device claim is present...
        if (
            self.keycloak_configuration.enable_device_authentication
            and self.keycloak_configuration.device_authentication_claim in token_info
        ):
            # ...only add the device auth claim to the claims to extract
            claims = [self.keycloak_configuration.device_authentication_claim]
            # If claim based authorization is enabled...
            if self.keycloak_configuration.authorization_method == AuthorizationMethod.CLAIM:
                # ...add the authorization claim to the claims to extract
                claims.append(self.keycloak_configuration.authorization_claim)

        # Extract claims from token
        user_info = {}
        for claim in claims:
            try:
                user_info[claim] = token_info[claim]
            except KeyError:
                log.warning("Claim %s is configured but missing in the token", claim)
                if self.keycloak_configuration.reject_on_missing_claim:
                    log.warning("Rejecting request because of missing claim")
                    raise AuthClaimMissing from KeyError
                log.debug("Backend is configured to ignore missing claims, continuing...")

        # Handle Authorization depending on the Claim Method
        scope_auth = []
        if (
            self.keycloak_configuration.authorization_method
            == AuthorizationMethod.CLAIM
        ):
            # Check if a path to the Claim is provided otherwise use the Claim directly
            if self.keycloak_configuration.authorization_claim_path:
                scope_auth = token_info
                for path in self.keycloak_configuration.authorization_claim_path:
                    try:
                        scope_auth = scope_auth[path]
                    except KeyError:
                        raise AuthClaimMissing
            else:
                if self.keycloak_configuration.authorization_claim not in token_info:
                    raise AuthClaimMissing
                scope_auth = token_info[self.keycloak_configuration.authorization_claim]

        # Check if the device authentication claim is present and evaluated to true
        # If so, the rest (mapping claims, user mapper, authorization) is skipped
        if self.keycloak_configuration.enable_device_authentication:
            log.debug("Device authentication is enabled, checking for device claim")
            try:
                if token_info[self.keycloak_configuration.device_authentication_claim]:
                    log.info("Request contains a device token, skipping user mapping")
                    return scope_auth, None
            except KeyError:
                log.debug(
                    "Device authentication claim is missing in the token, "
                    "proceeding with normal authentication"
                )

        # Call user function to get user object
        try:
            user = await self.get_user(user_info)
        except Exception as exc:
            log.warning(
                "Error while getting user object: %s. "
                "The user-provided function raised an exception",
                exc,
            )
            raise AuthUserError from exc

        if not user:
            log.warning("User object is None. The user-provided function returned None")
            raise AuthUserError

        return scope_auth, user
