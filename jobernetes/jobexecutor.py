"""
Class JobExecutor
"""

import yaml, os, time, datetime, urllib3
from datetime import timedelta
import kubernetes
from kubernetes import client, config
from kubernetes.client import configuration

import logging
import sys

class JobExecutor:
    def __init__(self,jobmodel,
                 namespace='default',
                 ssl_insecure_warnings=True,
                 cleanup=True,
                 refresh_time=5,
                 incluster=True,
                 parallelization=0):
        """
        Initialized JobExecutor(jobmodel)
        """
        self.log = logging.getLogger(__name__)
        self.log.debug('Initialized JobExecutor')
        self.jobmodel = jobmodel
        self.namespace = namespace
        self.cleanup = cleanup
        self.refresh_time = refresh_time
        self.incluster = incluster
        self.parallelization = parallelization
        if not ssl_insecure_warnings:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.__initialize_client()


    def start(self):
        the_end = False
        while not the_end:
            state = self.__get_phase()
            self.log.debug('Current phase is %i' % state)
            if state == -1:
                self.log.info('Creating first phase')
                #self.__create_phase(0)
                self.__update_phase(0)
            else:
                if self.__is_phase_finished(state):
                    if state == len(self.jobmodel)-1:
                        self.log.info('Finished!')
                        self.__report()
                        if self.cleanup:
                            self.__cleanup_jobs()
                        the_end = True
                        sys.exit(0) 
                    else:
                        #if not self.__is_phase_running(state+1):
                        self.log.info('Creating phase %s' % str(state+1))
                        #self.__create_phase(state+1)
                        self.__update_phase(state+1)
                else:
                    self.log.debug('phase %s is running' % str(state))
                    self.log.debug('Checking if depended jobs can be started')
                    self.__update_phase(state)
            self.log.debug("Waiting for %i seconds for status update" % self.refresh_time)
            time.sleep(self.refresh_time)
            #self.job_debug()
        exit(0)

    def __report(self):
        # Removed usertime feature since summation somehow is
        # not correct and raises a error.
        totaltime = timedelta(0)
        #start_times = []
        #end_times = []
        for job in self.__get_current_jobs().items:
            #start_times.append(job.status.start_time)
            #end_times.append(job.status.completion_time)
            time_taken = job.status.completion_time - job.status.start_time
            self.log.info('Job %s took %s' % (job.metadata.name,time_taken))
            totaltime += time_taken
        #user_time = sorted(end_times)[-1] - sorted(start_times)[0]
        self.log.info('Total computer runtime is %s' % totaltime)
        #self.log.info('Total user time is %s' % user_time)


    def __get_phase(self):
        """
        Gets current phase. It will go trough phases.
        :returns: int current phase (0 is not started)
        """
        job_list = self.__get_current_jobs().items
        current_phase = -1
        if len(job_list) == 0:
            return current_phase
        for i in range(len(self.jobmodel)):
            if len(self.__get_current_jobs(label_selector='jobernetes_phase='+str(i)).items) > 0:
                current_phase += 1
                self.log.debug('Found jobs in phase: %i' % i)
        return current_phase


    def __create_phase(self,phase_num,timeout=60):
        """
        Creates a phase.
        """
        for job in self.jobmodel[phase_num]['jobs']:
            if not self.__allowed_create_new_job():
               self.log.info('Cannot create more jobs since'
                             ' parallelization is %i' % self.parallelization)
               break

            if not 'depends_on' in job or len(job['depends_on']) == 0:
                self.kube_client.create_namespaced_job(body=job['kube_job_definition'],
                                                       namespace=self.namespace)
                self.log.info('Created job %s' % job['kube_job_definition']['metadata']['name'])


    def __update_phase(self,phase_num):
        jobs_to_be_created = []
        """
        checks if some jobs are finished so depended jobs can be started
        """
        for job in self.jobmodel[phase_num]['jobs']:
            # check if job is created then continue
            if self.__is_job_created(job,phase_num):
                continue
            if not self.__allowed_create_new_job():
               self.log.debug('Cannot create more jobs since'
                             ' parallelization is %i' % self.parallelization)
               break

            # check if non depended jobs are needed to be created
            if not 'depends_on' in job or len(job['depends_on']) == 0:
                self.kube_client.create_namespaced_job(body=job['kube_job_definition'],
                                                       namespace=self.namespace)
                self.log.info('Created job %s' % job['kube_job_definition']['metadata']['name'])
                if not self.__allowed_create_new_job():
                    self.log.info('Cannot create more jobs since'
                                  ' parallelization is %i' % self.parallelization)
                    break

            if 'depends_on' in job and len(job['depends_on']) > 0:
                self.log.debug('Checking dependencies of job: "%s"' % job['kube_job_definition']['metadata']['name'])
                if self.__are_dependencies_finished(job['depends_on']):
                    #jobs_to_be_created.append(job)
                    self.kube_client.create_namespaced_job(body=job['kube_job_definition'],
                                                           namespace=self.namespace)
                    self.log.info('dependencies of job: "%s" are done '
                                  'Creating new job.' % job['kube_job_definition']['metadata']['name'])
                    if not self.__allowed_create_new_job():
                        self.log.info('Cannot create more jobs since'
                                  ' parallelization is %i' % self.parallelization)
                        break


        #for job in jobs_to_be_created:
        #    self.kube_client.create_namespaced_job(body=job['kube_job_definition'],
        #                                           namespace=self.namespace)
        #    self.log.info('Created job: "%s"' % job['kube_job_definition']['metadata']['name'])


    def __allowed_create_new_job(self):
        if self.parallelization == 0:
            return True
        job_length = 0
        for job in self.__get_current_jobs().items:
            if bool(job.status.active):
                job_length += 1
        if job_length >= self.parallelization:
            return False
        return True


    def __is_phase_running(self,phase_num):
        """
        Should check if some of the jobs are created and active.  It should check
        """
        checklist = self.__get_current_jobs(label_selector="jobernetes_phase="+str(phase_num)).items
        if len(checklist) > 0:
            return True
        return False


    def __is_job_created(self,job,phase_num):
        for j in self.__get_current_jobs(label_selector="jobernetes_phase="+str(phase_num)).items:
            if job['kube_job_definition']['metadata']['name'] == j.metadata.name:
                return True
        return False


    def __is_job_finished(self,job):
        for j in self.__get_current_jobs().items:
            if job == j.metadata.name:
                self.log.debug('Found job %s, checking if finished' % job)
                return self.__job_finished_bool(j)
                #return not bool(j.status.active)
          #      try:
          #          c = j.status.completion_time
          #          return True
          #      except:
          #          # if unable to get completion time, job is not finished
          #          return False

    def __job_finished_bool(self,job):
        if job.status.succeeded == None:
            return False
        else:
            return True

    def __is_phase_finished(self,phase_num):
        for job in self.jobmodel[phase_num]['jobs']:
            if not self.__is_job_finished(job['kube_job_definition']['metadata']['name']):
                return False
        return True



    def __are_dependencies_finished(self,dep_array):
        is_finished = True
        for dep in dep_array:
            jobs = self.__get_current_jobs(label_selector="jobernetes_job_name="+dep).items
            if len(jobs) == 0:
                self.log.debug('No jobs are online that have jobernetes name: "%s"' % dep)
                is_finished = False
                continue
            for job in jobs:
                if not self.__job_finished_bool(job):
                    is_finished = False
                #if bool(job.status.active):
                #    self.log.debug('Job "%s" is not yet finished' % job.metadata.name)
                #    is_finished = False
        return is_finished



    def __initialize_client(self):
        """
        Current requires correct .kube/config and kubectl
        """
        #c = configuration.verify_ssl = False
        if self.incluster:
            config.load_incluster_config()
        else:
            config.load_kube_config()

        self.kube_client = client.BatchV1Api()



    def __get_current_jobs(self,label_selector=''):
        """
        Get listing of current jobs
        """
        return self.kube_client.list_namespaced_job(self.namespace,
                                                    _request_timeout=60,label_selector=label_selector)


    def __cleanup_jobs(self):
        phase_num = 0
        for phase in self.jobmodel:
            for job in phase['jobs']:
                if self.__is_job_created(job,phase_num):
                    self.kube_client.delete_namespaced_job(name=job['kube_job_definition']['metadata']['name'],
                                                            body={},
                                                            namespace=self.namespace)
                    self.log.info('Cleaning up job: "%s"' % job['kube_job_definition']['metadata']['name'])
            phase_num += 1




    def job_debug(self,label_selector=''):
        job_list = self.__get_current_jobs(label_selector)
        for job in job_list.items:
            print(job)
            print("%-40s%-15s%-40s%-30s%-25s" % (job.metadata.name,
                               bool(job.status.active),
                               job.spec.template.spec.containers[0].image,
                               job.status.start_time,
                               job.status.succeeded))



#    def __validate_phase(self,phase_num):
#        """
#        Phase should have all jobs are none.
#        :param: int phase_num
#        :returns: bool
#        """
#        current_job_names = self.__list_current_job_names(label_selector="jobernetes_phase="+str(phase_num))
#        for kube_job_name in self.jobmodel[phase_num]['jobs']:
#            if not kube_job_name['kube_job_definition']['metadata']['name'] in current_job_names:
#                return False
#        return True

#    def __list_current_job_names(self,label_selector=''):
#        name_list = []
#        for job in self.__get_current_jobs(label_selector).items:
#            name_list.append(job.metadata.name)
#        return name_list

#    def __is_job_created(self,job_name):
#        """
#        Checks if job is created
#        :param: str job_name required
#        :returns: Bool
#        """
#        for job in self.__get_current_jobs().items:
#            if job.metadata.name == job_name:
#                return True
#        return False
#
