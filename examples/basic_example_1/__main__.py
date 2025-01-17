'''Uniaxial extension of a bar.

Without sensitivity analysis.

Measurements
------------
- Measured displacement field on the top face.
- Measured reaction (tractions) on the right face.

Boundary conditions
-------------------
- Imposed displacements on the right face.
- Imposed zero-displacement on the left face.

'''

import os
import sys
import math
import logging
import numpy as np
import scipy.linalg as linalg
import matplotlib.pyplot as plt

import dolfin

from dolfin import Constant
from dolfin import DirichletBC
from dolfin import Expression
from dolfin import Function
from dolfin import assemble

import invsolve
import material

import examples.utility
import examples.plotting

logger = logging.getLogger()
logger.setLevel(logging.INFO)


### Problem parameters

FORCE_COST_FORMULATION_METHOD = "cost"
# FORCE_COST_FORMULATION_METHOD = "constraint"

NUM_OBSERVATIONS = 4

SMALL_DISPLACEMENTS = True

FINITE_ELEMENT_DEGREE = 1

PLOT_RESULTS = True
SAVE_RESULTS = True

PROBLEM_DIR = os.path.dirname(os.path.relpath(__file__))
RESULTS_DIR = os.path.join(PROBLEM_DIR, "results")

parameters_inverse_solver = {
    'solver_method': 'newton', # 'newton' or 'gradient'
    'sensitivity_method': 'adjoint', # 'adjoint' or 'direct'
    'maximum_iterations': 25,
    'maximum_divergences': 5,
    'absolute_tolerance': 1e-6,
    'relative_tolerance': 1e-6,
    'maximum_relative_change': None,
    'error_on_nonconvergence': False,
    'is_symmetric_form_dFdu': True,
    }


### Measurements (fabricated)

# Box problem domain
W, L, H = 2.0, 1.0, 1.0

# Maximum horizontal displacement of right-face
if SMALL_DISPLACEMENTS:
    uxD_max = 1e-5 # Small displacement case
else:
    uxD_max = 1e-1 # Large displacement case

# Fabricated model parameters
E_target, nu_target = 1.0, 0.3

# NOTE: The predicted model parameters will be close to the target model
# parameters when the displacements are small. This is consistent with the
# hyper-elastic model approaching the linear-elastic model in the limit of
# small strains. The difference between the solutions will be greater for
# larger displacements.

ex_max = uxD_max / W # Engineering strain
Tx_max = E_target * ex_max # Right-face traction

measurements_Tx  = np.linspace(0,  Tx_max, NUM_OBSERVATIONS+1)
measurements_uxD = np.linspace(0, uxD_max, NUM_OBSERVATIONS+1)

# Fabricate top-face boundary displacement field measurements
u_msr = Expression(('ex*x[0]', '-nu*ex*x[1]', '-nu*ex*x[2]'),
                   ex=0.0, nu=nu_target, degree=1)

# Right-face boundary traction measurements
T_msr = Expression(('value_x', '0.0', '0.0'),
                   value_x=0.0, degree=0)

# Right-face boundary displacements measurements
uxD_msr = Expression('value', value=0.0, degree=0)

def measurement_setter(i):
    '''Set measurements at index `i`.'''
    T_msr.value_x = measurements_Tx[i]
    u_msr.ex = measurements_uxD[i] / W
    uxD_msr.value = measurements_uxD[i]

using_subdims_u_msr = [0, 1] # [0, 1, 2]
using_subdims_T_msr = [0]


### Mesh

nz = 10
nx = max(int(nz*W/H), 1)
ny = max(int(nz*L/H), 1)

mesh = dolfin.BoxMesh(dolfin.Point(0,0,0), dolfin.Point(W,L,H), nx, ny, nz)

# Define the fixed boundaries and measurement subdomains

boundary_fix = dolfin.CompiledSubDomain(f'on_boundary && near(x[0], {0.0})')
boundary_msr = dolfin.CompiledSubDomain(f'on_boundary && near(x[0], {W})')
boundary_dic = dolfin.CompiledSubDomain(f'on_boundary && near(x[1], {L})')

fixed_vertex_000 = dolfin.CompiledSubDomain(
    f'near(x[0], {0.0}) && near(x[1], {0.0}) && near(x[2], {0.0})')

fixed_vertex_010 = dolfin.CompiledSubDomain(
    f'near(x[0], {0.0}) && near(x[1], {L}) && near(x[2], {0.0})')

# Mark the elemental entities (e.g. cells, facets) belonging to the subdomains

domain_dim = mesh.geometry().dim()
boundary_dim = domain_dim - 1

boundary_markers = dolfin.MeshFunction('size_t', mesh, boundary_dim)
boundary_markers.set_all(0) # Assign all elements the default value

id_subdomain_fix = 1 # Fixed boundary id
id_subdomain_msr = 2 # Loaded boundary id
id_subdomains_dic = 3 # displacement field measurement boundary id

boundary_fix.mark(boundary_markers, id_subdomain_fix)
boundary_msr.mark(boundary_markers, id_subdomain_msr)
boundary_dic.mark(boundary_markers, id_subdomains_dic)


### Integration measures

dx = dolfin.dx(domain=mesh) # for the whole domain
ds = dolfin.ds(domain=mesh) # for the entire boundary

ds_msr_T = dolfin.Measure('ds', mesh,
    subdomain_id=id_subdomain_msr,
    subdomain_data=boundary_markers)

ds_msr_u = dolfin.Measure('ds', mesh,
    subdomain_id=id_subdomains_dic,
    subdomain_data=boundary_markers)


### Finite element function spaces

V = dolfin.VectorFunctionSpace(mesh, 'CG', FINITE_ELEMENT_DEGREE)

# Displacement field
u = Function(V)


### Dirichlet boundary conditions

bcs = []

Vx, Vy, Vz = V.split()

zero  = Constant(0)
zeros = Constant((0,0,0))

bcs.append(DirichletBC(Vx, zero, boundary_markers, id_subdomain_fix))
bcs.append(DirichletBC(Vx, uxD_msr, boundary_markers, id_subdomain_msr))

bcs.append(DirichletBC(V, zeros, fixed_vertex_000, "pointwise"))
bcs.append(DirichletBC(Vz, zero, fixed_vertex_010, "pointwise"))


### Define hyperelastic material model

material_parameters = {'E': Constant(1.0),
                       'nu': Constant(0.0)}

E, nu = material_parameters.values()

d = len(u) # Displacement dimension

I = dolfin.Identity(d)
F = dolfin.variable(I + dolfin.grad(u))

C  = F.T*F
J  = dolfin.det(F)
I1 = dolfin.tr(C)

# Lame material parameters
lm = E*nu/((1.0 + nu)*(1.0 - 2.0*nu))
mu = E/(2.0 + 2.0*nu)

# Energy density of a Neo-Hookean material model
psi = (mu/2.0) * (I1 - d - 2.0*dolfin.ln(J)) + (lm/2.0) * dolfin.ln(J) ** 2

# First Piola-Kirchhoff
pk1 = dolfin.diff(psi, F)

# Boundary traction
N = dolfin.FacetNormal(mesh)
PN = dolfin.dot(pk1, N)

# Potential energy
Pi = psi*dx # NOTE: There is no external force potential

# Equilibrium problem
F = dolfin.derivative(Pi, u)


### Model cost

# Observed displacement
u_obs = u # NOTE: Generally a vector-valued sub-function

# Observed tractions
T_obs = PN # NOTE: Generally a sequence of vector-valued tractions

# Displacement misfit cost
J_u = sum((u_obs[i]-u_msr[i])**2*ds_msr_u for i in using_subdims_u_msr)

# Reaction force misfit
C = [(T_obs[i]-T_msr[i])*ds_msr_T for i in using_subdims_T_msr]

if FORCE_COST_FORMULATION_METHOD == "cost":

    constraint_multipliers = []

    Q = J_u
    L = C[0]

    # NOTE: The final objective to be minimized will effectively be like:
    # J = Q + 0.5*L*L

elif  FORCE_COST_FORMULATION_METHOD == "constraint":

    constraint_multipliers = [Constant(1e-9) for _ in using_subdims_T_msr]
    J_c = sum(mult_i*C_i for mult_i, C_i in zip(constraint_multipliers, C))

    Q = J_u + J_c
    L = None

else:
    raise ValueError('Parameter `FORCE_COST_FORMULATION_METHOD ')


### Inverse problem

model_parameters = [material_parameters]
model_parameters.append(constraint_multipliers)
observation_times = range(1, NUM_OBSERVATIONS+1)

inverse_solver_basic = invsolve.InverseSolverBasic(Q, L, F, u, bcs,
    model_parameters, observation_times, measurement_setter)

inverse_solver = invsolve.InverseSolver(inverse_solver_basic,
    u_obs, u_msr, ds_msr_u, T_obs, T_msr, ds_msr_T)

inverse_solver.set_parameters_inverse_solver(parameters_inverse_solver)


### Solve inverse problem

cost_values_initial = cost_gradients_initial = None

# cost_values_initial, cost_gradients_initial = \
#     inverse_solver.assess_model_cost(compute_gradients=False)

model_parameters_foreach, iterations_count_foreach, is_converged_foreach = \
    inverse_solver.fit_model_foreach_time() # Default observation times

model_parameters_forall, iterations_count_forall, is_converged_forall = \
    inverse_solver.fit_model_forall_times() # Default observation times

cost_values_final, cost_gradients_final = \
    inverse_solver.assess_model_cost(compute_gradients=True)


### Mismatch between model and measurements

misfit_displacements = inverse_solver \
    .assess_misfit_displacements(observation_times, using_subdims_u_msr)
# NOTE: Value at `[I][J]` corresponds to the `I`th measurement, `J`th time.

misfit_reaction_forces = inverse_solver \
    .assess_misfit_reaction_forces(observation_times, using_subdims_T_msr)
# NOTE: Value at `[I][J]` corresponds to the `I`th measurement, `J`th time.


### Force-displacement curve

reaction_forces_observed = inverse_solver.observe_f_obs(observation_times)
reaction_forces_measured = inverse_solver.observe_f_msr(observation_times)
# NOTE: Value at `[I][J][K]` corresponds to the `I`th measurement, `J`th time,
#       `K`th force dimension.


### Assess cost condition number

D2JDm2 = inverse_solver.view_cumsum_D2JDm2()
cond_D2JDm2 = np.linalg.cond(D2JDm2)


### Plotting

# Model parameter names to be used in labeling plots
model_parameter_names = list(material_parameters.keys())

if len(constraint_multipliers) > 1:
    model_parameter_names.extend([f'constraint_multiplier_{i}'
        for i in range(1, len(constraint_multipliers)+1)])
elif len(constraint_multipliers) == 1:
    model_parameter_names.append('constraint_multiplier')

def plot_everything():

    plt.close('all')

    fig_handle_and_name_pairs = []

    fig_handle_and_name_pairs.append(
        examples.plotting.plot_model_parameters_foreach(
            model_parameters_foreach,
            model_parameter_names,
            observation_times,
            figname="Fitted Model Parameters for Each Observation Time"))

    fig_handle_and_name_pairs.append(
        examples.plotting.plot_model_parameters_forall(
            model_parameters_forall,
            model_parameter_names,
            figname="Fitted Model Parameters for all Observation Times"))

    fig_handle_and_name_pairs.append(
        examples.plotting.plot_model_cost(
            cost_values_final,
            cost_values_initial,
            observation_times,
            figname="Model Cost"))

    fig_handle_and_name_pairs.append(
        examples.plotting.plot_cost_gradients(
            cost_gradients_final,
            model_parameter_names,
            observation_times,
            figname="Model Cost Derivatives"))

    fig_handle_and_name_pairs.append(
        examples.plotting.plot_observation_misfit(
            misfit_reaction_forces_i,
            observation_times,
            figname="Reaction Force Misfit Error",
            ylabel="Reaction force misfit error, $||f_{obs}-f_{msr}||/||f_{msr}||$"))

    fig_handle_and_name_pairs.append(
        examples.plotting.plot_observation_misfit(
            misfit_displacements_i,
            observation_times,
            figname="Displacement Field Misfit Error",
            ylabel="Displacement field misfit error, $||u_{obs}-u_{msr}||/||u_{msr}||$"))

    fig_handle_and_name_pairs.append(
        examples.plotting.plot_reaction_force_vs_displacement(
            reaction_force_magnitude_observed_i,
            reaction_force_magnitude_measured_i,
            reaction_displacement_magnitude_i,
            figname="Reaction Force-Displacement Curve"))

    return fig_handle_and_name_pairs


if __name__ == '__main__':

    plt.interactive(True)

    i_msr_u = 0 # Assess first displacement field measurements
    i_msr_f = 0 # Assess first reaction force measurements
    i_time = -1 # Assess last observation time

    misfit_displacements_i = misfit_displacements[i_msr_u]
    misfit_reaction_forces_i = misfit_reaction_forces[i_msr_f]

    reaction_force_observed_i = reaction_forces_observed[i_msr_f]
    reaction_force_measured_i = reaction_forces_measured[i_msr_f]

    reaction_force_magnitude_observed_i = np.sqrt(np.array(
        reaction_force_observed_i)**2).sum(axis=1).tolist()

    reaction_force_magnitude_measured_i = np.sqrt(np.array(
        reaction_force_measured_i)**2).sum(axis=1).tolist()

    reaction_displacement_magnitude_i = \
        [measurements_uxD[t] for t in observation_times]

    print(f'\nmodel_parameters_foreach (converged={all(is_converged_foreach)}):')
    for t, r in zip(observation_times, np.array(model_parameters_foreach)):
        print(r, end=' '); print(f'[t={t}]')

    print(f'\nmodel_parameters_forall (converged={is_converged_forall}):')
    print(np.array(model_parameters_forall))

    print(f'\nerror_displacements (subdims={using_subdims_u_msr}):')
    for t, v in zip(observation_times, misfit_displacements_i):
        print(f'{v:12.5e} [t={t}]')

    print(f'\nerror_reaction_forces (subdims={using_subdims_T_msr}):')
    for t, v in zip(observation_times, misfit_reaction_forces_i):
        print(f'{v:12.5e} [t={t}]')

    print('\ncond(D2JDm2):')
    print(f'{cond_D2JDm2:.5e}')

    print(f'\nnorm(u):')
    print(f'{dolfin.norm(u):.5e}')

    if PLOT_RESULTS or SAVE_RESULTS:

        fig_handle_and_name_pairs = plot_everything()
        fig_handles = [f[0] for f in fig_handle_and_name_pairs]
        fig_names = [f[1] for f in fig_handle_and_name_pairs]

        if SAVE_RESULTS:

            if not os.path.isdir(RESULTS_DIR):
                os.makedirs(RESULTS_DIR)

            for handle_i, name_i in zip(fig_handles, fig_names):
                handle_i.savefig(os.path.join(RESULTS_DIR, name_i)+'.png')
                handle_i.savefig(os.path.join(RESULTS_DIR, name_i)+'.pdf')

            if not PLOT_RESULTS:
                plt.close('all')

            outfile = dolfin.File(os.path.join(RESULTS_DIR,'pvd','u.pvd'))
            for t in inverse_solver.observation_times:
                outfile << inverse_solver.observe_u(t, copy=False)
