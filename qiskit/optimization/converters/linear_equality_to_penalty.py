# -*- coding: utf-8 -*-

# This code is part of Qiskit.
#
# (C) Copyright IBM 2020.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Converter to convert a problem with equality constraints to unconstrained with penalty terms."""

import copy
from typing import Optional, cast, Union, Tuple, Dict
from math import fsum
import logging

from ..algorithms.optimization_algorithm import OptimizationResult, OptimizationResultStatus
from ..problems.quadratic_program import QuadraticProgram, QuadraticProgramStatus
from ..problems.variable import Variable
from ..problems.constraint import Constraint
from ..problems.quadratic_objective import QuadraticObjective
from ..exceptions import QiskitOptimizationError

logger = logging.getLogger(__name__)


class LinearEqualityToPenalty:
    """Convert a problem with only equality constraints to unconstrained with penalty terms."""

    def __init__(self, penalty: Optional[float] = None, name: Optional[str] = None):
        """
        Args:
            penalty: Penalty factor to scale equality constraints that are added to objective.
                     If None is passed, penalty factor will be automatically calculated.
            name: The name of the converted problem.
        """
        self._src = None  # type: Optional[QuadraticProgram]
        self._dst = None  # type: Optional[QuadraticProgram]
        self._dst_name = name  # type: Optional[str]
        self._penalty = penalty  # type: Optional[float]

    def encode(self, op: QuadraticProgram) -> QuadraticProgram:
        """Convert a problem with equality constraints into an unconstrained problem.

        Args:
            op: The problem to be solved, that does not contain inequality constraints.

        Returns:
            The converted problem, that is an unconstrained problem.

        Raises:
            QiskitOptimizationError: If an inequality constraint exists.
        """

        # create empty QuadraticProgram model
        self._src = copy.deepcopy(op)  # deep copy
        self._dst = QuadraticProgram()

        # If penalty is None, set the penalty coefficient by _auto_define_penalty()
        if self._penalty is None:
            penalty = self._auto_define_penalty()
        else:
            penalty = self._penalty

        # set problem name
        if self._dst_name is None:
            self._dst.name = self._src.name
        else:
            self._dst.name = self._dst_name

        # set variables
        for x in self._src.variables:
            if x.vartype == Variable.Type.CONTINUOUS:
                self._dst.continuous_var(x.lowerbound, x.upperbound, x.name)
            elif x.vartype == Variable.Type.BINARY:
                self._dst.binary_var(x.name)
            elif x.vartype == Variable.Type.INTEGER:
                self._dst.integer_var(x.lowerbound, x.upperbound, x.name)
            else:
                raise QiskitOptimizationError('Unsupported vartype: {}'.format(x.vartype))

        # get original objective terms
        offset = self._src.objective.constant
        linear = self._src.objective.linear.to_dict()
        quadratic = self._src.objective.quadratic.to_dict()
        sense = self._src.objective.sense.value

        # convert linear constraints into penalty terms
        for constraint in self._src.linear_constraints:

            if constraint.sense != Constraint.Sense.EQ:
                raise QiskitOptimizationError(
                    'An inequality constraint exists. '
                    'The method supports only equality constraints.'
                )

            constant = constraint.rhs
            row = constraint.linear.to_dict()

            # constant parts of penalty*(Constant-func)**2: penalty*(Constant**2)
            offset += sense * penalty * constant ** 2

            # linear parts of penalty*(Constant-func)**2: penalty*(-2*Constant*func)
            for j, coef in row.items():
                # if j already exists in the linear terms dic, add a penalty term
                # into existing value else create new key and value in the linear_term dict
                linear[j] = linear.get(j, 0.0) + sense * penalty * -2 * coef * constant

            # quadratic parts of penalty*(Constant-func)**2: penalty*(func**2)
            for j, coef_1 in row.items():
                for k, coef_2 in row.items():
                    # if j and k already exist in the quadratic terms dict,
                    # add a penalty term into existing value
                    # else create new key and value in the quadratic term dict

                    # according to implementation of quadratic terms in OptimizationModel,
                    # don't need to multiply by 2, since loops run over (x, y) and (y, x).
                    tup = cast(Union[Tuple[int, int], Tuple[str, str]], (j, k))
                    quadratic[tup] = quadratic.get(tup, 0.0) + sense * penalty * coef_1 * coef_2

        if self._src.objective.sense == QuadraticObjective.Sense.MINIMIZE:
            self._dst.minimize(offset, linear, quadratic)
        else:
            self._dst.maximize(offset, linear, quadratic)

        return self._dst

    def _auto_define_penalty(self) -> float:
        """Automatically define the penalty coefficient.

        Returns:
            Return the minimum valid penalty factor calculated
            from the upper bound and the lower bound of the objective function.
            If a constraint has a float coefficient,
            return the default value for the penalty factor.
        """
        default_penalty = 1e5

        # Check coefficients of constraints.
        # If a constraint has a float coefficient, return the default value for the penalty factor.
        terms = []
        for constraint in self._src.linear_constraints:
            terms.append(constraint.rhs)
            terms.extend(coef for coef in constraint.linear.to_dict().values())
        if any(isinstance(term, float) and not term.is_integer() for term in terms):
            logger.warning(
                'Warning: Using %f for the penalty coefficient because '
                'a float coefficient exists in constraints. \n'
                'The value could be too small. '
                'If so, set the penalty coefficient manually.',
                default_penalty,
            )
            return default_penalty

        # (upper bound - lower bound) can be calculate as the sum of absolute value of coefficients
        # Firstly, add 1 to guarantee that infeasible answers will be greater than upper bound.
        penalties = [1.0]
        # add linear terms of the object function.
        penalties.extend(abs(coef) for coef in self._src.objective.linear.to_dict().values())
        # add quadratic terms of the object function.
        penalties.extend(abs(coef) for coef in self._src.objective.quadratic.to_dict().values())

        return fsum(penalties)

    def decode(self, result: OptimizationResult) -> OptimizationResult:
        """Convert the result of the converted problem back to that of the original problem
        Args:
            result: The result of the converted problem.

        Returns:
            The result of the original problem.

        Raises:
            QiskitOptimizationError: if the number of variables in the result differs from
                                     that of the original problem.
        """
        if len(result.x) != len(self._src.variables):
            raise QiskitOptimizationError(
                'The number of variables in the passed result differs from '
                'that of the original problem.'
            )
        # Substitute variables to obtain the function value and feasibility in the original problem
        substitute_dict = {}  # type: Dict[Union[str, int], float]
        variables = self._src.variables
        for i in range(len(result.x)):
            substitute_dict[variables[i].name] = result.x[i]
        substituted_qp = self._src.substitute_variables(substitute_dict)

        new_result = OptimizationResult()
        new_result.x = result.x

        # Set the new function value
        new_result.fval = substituted_qp.objective.constant

        # Set the new status of optimization result
        if substituted_qp.status == QuadraticProgramStatus.VALID:
            new_result.status = OptimizationResultStatus.SUCCESS
        else:
            new_result.status = OptimizationResultStatus.INFEASIBLE

        return new_result

    @property
    def penalty(self) -> Optional[float]:
        """Returns the penalty factor used in conversion.

        Returns:
            The penalty factor used in conversion.
        """
        return self._penalty

    @penalty.setter  # type:ignore
    def penalty(self, penalty: Optional[float]) -> None:
        """Set a new penalty factor.

        Args:
            penalty: The new penalty factor.
                     If None is passed, penalty factor will be automatically calculated.
        """
        self._penalty = penalty

    @property
    def name(self) -> Optional[str]:
        """Returns the name of the converted problem

        Returns:
            The name of the converted problem
        """
        return self._dst_name

    @name.setter  # type:ignore
    def name(self, name: Optional[str]) -> None:
        """Set a name for a converted problem

        Args:
            name: A name for a converted problem. If not provided, the name of the input
                  problem is used.
        """
        self._dst_name = name
