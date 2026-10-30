pipeline {
    agent {
        label 'amzlnx2'
    }

    options {
        ansiColor('xterm')
        disableConcurrentBuilds()
    }
    environment {
        SHARED_ACCOUNT = "278833423079"
        ECR_REPO_NAME = "opta/chewy-genai-automation"
        REGION="us-east-1"
    }
    stages {

        stage('build') {
          steps {
            script {
              // read git commit hash to use as version
              def shortCommit = sh(returnStdout: true, script: "git log -n 1 --pretty=format:'%h'").trim()
              sh """
                  echo "version=${shortCommit}"
              """
              // write the version to a file for post step
              writeFile(file: 'version.txt', text: shortCommit)
              // build docker image and tag it with the version
              sh """#!/bin/bash
                    ${ecrLogin()}
                    docker --version
                    
                    # Enable BuildKit for optimizations
                    export DOCKER_BUILDKIT=1
                    
                    # Build with BuildKit optimizations (cache mounts work with default driver)
                    docker buildx build \\
                        --platform "linux/amd64" \\
                        -t ${env.SHARED_ACCOUNT}.dkr.ecr.${env.REGION}.amazonaws.com/${env.ECR_REPO_NAME}:${shortCommit} .
              """
            }
          }
        }

        stage('docker push') {
            steps {
                withCredentials([usernamePassword(credentialsId: 'jenkins-github-userpass',
                                                  passwordVariable: 'GRGIT_PASS',
                                                  usernameVariable: 'GRGIT_USER')]) {

                    script {
                        sh """#!/bin/bash
                              ${ecrLogin()}
                              docker push --all-tags ${env.SHARED_ACCOUNT}.dkr.ecr.${env.REGION}.amazonaws.com/${env.ECR_REPO_NAME}
                        """
                    }
                }
            }
        }
    }

    post {
        success {
            script {
                def version = readFile "version.txt"
                // Change to displayName if you want it in the title
                currentBuild.description = "${version}"
            }
        }
    }
}